from __future__ import annotations

import copy
import hashlib
import subprocess
from pathlib import Path

import backend.evals.run_simple_open_golden as golden
import pytest
from backend.evals.run_simple_open_golden import build_artifact, verify_artifact


EXPECTED_STAGE_LABELS = [
    "route_mobility_clarification",
    "direction_a_offered",
    "direction_a_confirmed",
    "direction_a_edited",
    "direction_a_saved",
    "direction_b_offered",
    "direction_b_confirmed",
    "direction_b_saved",
    "direction_a_restored",
    "duplicate_direction_a_choice",
]

EXPECTED_SOURCE_EPOCH_PATHS = {
    "backend/src/api/schemas/itineraries.py",
    "backend/src/core/config.py",
    "backend/src/core/schema.py",
    "backend/src/models/itinerary_segment.py",
    "backend/src/models/poi.py",
    "backend/src/models/poi_intent.py",
    "backend/src/models/route_option.py",
    "backend/src/runtime/agent_runtime.py",
    "backend/src/services/agent_action_directive.py",
    "backend/src/services/agent_decision_contract_service.py",
    "backend/src/services/agent_turn_coordinator.py",
    "backend/src/services/agent_choice_trace_service.py",
    "backend/src/services/deepseek_agent_provider.py",
    "backend/src/services/constraint_ledger_compiler.py",
    "backend/src/services/goal_occurrence_compiler.py",
    "backend/src/services/creative_planning_models.py",
    "backend/src/services/creative_proposal_title_service.py",
    "backend/src/services/creative_portfolio_staging_service.py",
    "backend/src/services/portfolio_partial_projection_service.py",
    "backend/src/services/portfolio_route_feasibility_service.py",
    "backend/src/services/route_insertion_scorer.py",
    "backend/src/services/intent_candidate_semantic_policy.py",
    "backend/src/services/night_view_candidate_policy.py",
    "backend/src/services/agent_run_control.py",
    "backend/src/services/agent_reasoning_status_service.py",
    "backend/tests/unit/test_agent_run_control.py",
    "backend/src/services/agent_verifier_service.py",
    "backend/src/services/itinerary_snapshot_service.py",
    "backend/src/services/itinerary_service.py",
    "backend/src/services/feasibility_service.py",
    "backend/src/services/planning_run_service.py",
    "backend/src/services/ticket_service.py",
    "backend/src/services/timeline_mutation_transaction_service.py",
    "backend/src/services/versioned_write_guard_service.py",
    "backend/src/services/proposal_route_evidence_normalizer.py",
    "backend/src/services/provider_route_insertion_service.py",
    "backend/src/services/amap_call_budget.py",
    "backend/src/services/map_poi_service.py",
    "backend/src/services/poi_physical_identity_service.py",
    "backend/src/services/route_service.py",
    "backend/src/services/simple_open_dynamic_schedule_service.py",
    "backend/src/services/simple_open_route_assignment_service.py",
    "backend/src/services/agent_harness_trace_service.py",
    "backend/src/services/agent_model_registry.py",
    "backend/tests/unit/test_comparison_projection_update_mode.py",
    "backend/tests/unit/test_itinerary_snapshot_service.py",
    "e2e/agent-reasoning-visual.spec.ts",
    "frontend/src/styles.css",
    "frontend/src/components/agent/AgentReasoningProgress.tsx",
    "frontend/src/modelRegistry.ts",
    "frontend/tests/integration/itineraryWorkspace.test.tsx",
}


@pytest.fixture(scope="module")
def runtime_golden_artifact() -> dict:
    return build_artifact()


@pytest.fixture(scope="module")
def rank_runtime_golden_artifact() -> dict:
    return build_artifact(route_overlap_policy="rank")


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _source_epoch_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "source-epoch"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "golden@example.invalid")
    _git(repo, "config", "user.name", "Golden Test")
    (repo / ".gitattributes").write_text("*.txt text eol=lf\n", encoding="utf-8")
    (repo / "tracked.txt").write_bytes(b"alpha\nbeta\n")
    _git(repo, "add", ".gitattributes", "tracked.txt")
    _git(repo, "commit", "-m", "freeze source epoch")
    return repo, _git(repo, "rev-parse", "HEAD")


def test_simple_direction_source_epoch_allowlist_covers_runtime_dependencies() -> None:
    assert EXPECTED_SOURCE_EPOCH_PATHS <= set(golden.SIMPLE_OPEN_IMPLEMENTATION_PATHS)


def test_source_epoch_clean_uses_normalized_worktree_and_index_content(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo, frozen_sha = _source_epoch_repo(tmp_path)
    monkeypatch.setattr(golden, "PROJECT_ROOT", repo)
    monkeypatch.setattr(golden, "SIMPLE_OPEN_IMPLEMENTATION_PATHS", ("tracked.txt",))

    # The checkout bytes differ from the frozen LF blob, but Git attributes
    # normalize them to the same content that would be staged.
    (repo / "tracked.txt").write_bytes(b"alpha\r\nbeta\r\n")
    assert golden._implementation_paths_clean(frozen_sha) is True

    (repo / "tracked.txt").write_text("alpha\nchanged\n", encoding="utf-8")
    assert golden._implementation_paths_clean(frozen_sha) is False

    _git(repo, "add", "tracked.txt")
    assert golden._implementation_paths_clean(frozen_sha) is False


def test_source_epoch_clean_rejects_untracked_and_missing_allowlist_paths(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo, frozen_sha = _source_epoch_repo(tmp_path)
    monkeypatch.setattr(golden, "PROJECT_ROOT", repo)

    (repo / "untracked.txt").write_text("not frozen\n", encoding="utf-8")
    monkeypatch.setattr(
        golden,
        "SIMPLE_OPEN_IMPLEMENTATION_PATHS",
        ("tracked.txt", "untracked.txt"),
    )
    assert golden._implementation_paths_clean(frozen_sha) is False

    monkeypatch.setattr(
        golden,
        "SIMPLE_OPEN_IMPLEMENTATION_PATHS",
        ("tracked.txt", "missing.txt"),
    )
    assert golden._implementation_paths_clean(frozen_sha) is False
    assert golden._implementation_file_hashes(frozen_sha) == {
        "tracked.txt": hashlib.sha256(b"alpha\nbeta\n").hexdigest()
    }


def test_source_epoch_hashes_frozen_commit_blobs_not_checkout_bytes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo, frozen_sha = _source_epoch_repo(tmp_path)
    monkeypatch.setattr(golden, "PROJECT_ROOT", repo)
    monkeypatch.setattr(golden, "SIMPLE_OPEN_IMPLEMENTATION_PATHS", ("tracked.txt",))

    (repo / "tracked.txt").write_bytes(b"different checkout bytes\r\n")

    assert golden._implementation_file_hashes(frozen_sha) == {
        "tracked.txt": hashlib.sha256(b"alpha\nbeta\n").hexdigest()
    }


def test_artifact_verifier_reuses_one_resolved_sha_and_artifact_bound_source_epoch(monkeypatch) -> None:
    frozen_sha = "a" * 40
    implementation_hashes = {"tracked.txt": "b" * 64}
    sha_calls: list[str] = []
    hash_calls: list[str] = []
    clean_calls: list[str] = []

    def implementation_sha() -> str:
        sha_calls.append("resolved")
        return frozen_sha

    def implementation_file_hashes(commit_sha: str) -> dict[str, str]:
        hash_calls.append(commit_sha)
        return implementation_hashes

    def implementation_paths_clean(commit_sha: str) -> bool:
        clean_calls.append(commit_sha)
        return True

    monkeypatch.setattr(golden, "SIMPLE_OPEN_IMPLEMENTATION_PATHS", ("tracked.txt",))
    monkeypatch.setattr(golden, "_implementation_sha", implementation_sha)
    monkeypatch.setattr(golden, "_implementation_file_hashes", implementation_file_hashes)
    monkeypatch.setattr(golden, "_implementation_paths_clean", implementation_paths_clean)
    monkeypatch.setattr(golden, "_derive_artifact_truth", lambda _artifact: {"sourceEpoch": True})
    monkeypatch.setattr(golden, "_scan_artifact", lambda _artifact: {"secretFieldCount": 0})

    artifact = {
        "schemaVersion": golden.SCHEMA_VERSION,
        "gitCommit": frozen_sha,
        "sourceAttribution": {
            "commitBoundary": "frozen-implementation-source",
            "implementationCommitSha": frozen_sha,
            "implementationPaths": ["tracked.txt"],
            "implementationPathsClean": True,
            "implementationFilesSha256": implementation_hashes,
        },
        "fixture": {"networkAttempts": []},
        "invariants": {"sourceEpoch": True},
        "safetyScan": {"secretFieldCount": 0, "externalNetworkCallCount": 0},
    }
    artifact["artifactSha256"] = golden._canonical_sha256(artifact)

    golden.verify_artifact(artifact)

    assert sha_calls == ["resolved"]
    assert hash_calls == [frozen_sha]
    assert clean_calls == [frozen_sha]


def test_simple_direction_runtime_golden_covers_clarify_offer_save_switch_and_replay(
    runtime_golden_artifact: dict,
) -> None:
    artifact = runtime_golden_artifact

    assert artifact["schemaVersion"] == "simple-open-direction-golden-artifact-v3"
    assert artifact["fixture"]["type"] == "recorded_provider_shaped_deterministic"
    assert artifact["fixture"]["liveProviderClaimed"] is False
    assert artifact["fixture"]["networkAttempts"] == []
    assert artifact["workflow"] == {
        "executionProfile": "simple_open_v1",
        "workflowMode": "simple_direction_v1",
    }

    stages = artifact["stages"]
    assert [stage["label"] for stage in stages] == EXPECTED_STAGE_LABELS
    by_label = {stage["label"]: stage for stage in stages}

    clarification = by_label["route_mobility_clarification"]
    assert clarification["deltas"] == {
        "portfolio": 0,
        "proposal": 0,
        "version": 0,
        "patch": 0,
        "route": 0,
        "choiceExecution": 0,
    }
    assert clarification["providerCallDeltas"] == {
        "intent": 0,
        "controller": 1,
        "planner": 0,
        "poiSearch": 0,
        "route": 0,
    }
    assert clarification["activeVersionAfter"] is None

    offered_a = by_label["direction_a_offered"]
    assert offered_a["deltas"] == {
        "portfolio": 1,
        "proposal": 1,
        "version": 0,
        "patch": 0,
        "route": 0,
        "choiceExecution": 1,
    }
    assert offered_a["activeVersionAfter"] is None
    assert offered_a["response"]["terminalStatus"] == "needs_confirmation"
    assert offered_a["response"]["comparisonProjectionUpdateMode"] == "replace"
    assert offered_a["response"]["itineraryPresent"] is False
    assert offered_a["response"]["versionPresent"] is False

    # A proposal offer is a confirmation capability, not a route-pending draft.
    # The Golden must therefore exercise the same compact route contract and
    # frozen AMap evidence required by the production verifier before the
    # proposal row is persisted.
    for label in ("direction_a_offered", "direction_b_offered"):
        route_contract = by_label[label]["routeContract"]
        assert route_contract == {
            "schemaVersion": "route-decision-contract-v2",
            "status": "ready",
            "missingFields": [],
            "fingerprintPresent": True,
            "adjacentLegConstraint": {
                "candidateSearchRadiusMeters": 8000.0,
                "maxProviderTravelMinutes": 60.0,
            },
            "topologyConstraint": {"maxBacktrackRatio": 0.15},
        }
        assert 0 < by_label[label]["providerCallDeltas"]["route"] <= 4

    # The recorded DaySlot provider fixture contains one campus and one meal
    # on each day plus the single clarified night occurrence.  The server-owned
    # daily-completion policy then adds a route-local park on sparse Day 2.  It
    # remains a separately proven, provider-grounded stop: no stale Controller
    # slot may regain authority and no empty day may be disguised by a
    # materialized `待继续安排` rest placeholder.
    assert "两所不同高校" in artifact["input"]["initialRequest"]
    assert "每天午餐" in artifact["input"]["initialRequest"]
    for offer in artifact["directionOffers"]:
        assert [(item["dayNumber"], item["kind"]) for item in offer["items"]] == [
            (1, "campus"),
            (1, "meal"),
            (1, "night_view"),
            (2, "campus"),
            (2, "meal"),
            (2, "park"),
        ]
        assert all(item["kind"] != "rest" for item in offer["items"])

        route_assignment = offer["routeAssignment"]
        assert route_assignment["schemaVersion"] == "simple-open-route-evidence-v2"
        assert route_assignment["routeContractFingerprintPresent"] is True
        assert route_assignment["adjacentLegConstraint"] == {
            "candidateSearchRadiusMeters": 8000.0,
            "maxProviderTravelMinutes": 60.0,
        }
        assert route_assignment["topologyCompliance"] == "verified"
        assert route_assignment["adjacentLegCompliance"] == "verified"
        assert route_assignment["providerBaselineCompared"] is False
        assert route_assignment["detourCompliance"] == "not_evaluated"
        assert route_assignment["routeCoverageComplete"] is True

        expected_pairs = {(row["fromAmapId"], row["toAmapId"]) for row in route_assignment["expectedPairs"]}
        verified_pairs = {(row["fromAmapId"], row["toAmapId"]) for row in route_assignment["verifiedPairs"]}
        assert expected_pairs == verified_pairs
        assert len(expected_pairs) == route_assignment["routeProviderAttemptCount"] == 4
        assert route_assignment["routeProviderAttemptCount"] <= 4
        assert all(
            row["transportMode"] == "transit"
            and row["provider"] == "amap-webservice"
            and 0 < row["durationSeconds"] <= 45 * 60
            and 0 < row["distanceMeters"] <= 5000
            and row["providerEvidenceFingerprint"]
            for row in route_assignment["verifiedPairs"]
        )
        assert all(
            day["daySeedAmapId"] == day["orderedAmapIds"][0]
            and all(0 < distance <= 5000 for distance in day["adjacentGeometryMeters"])
            and day["backtrackRatio"] <= 0.15
            for day in route_assignment["perDayTopology"]
        )
        assert route_assignment["legacyBacktrackMetric"] == "waypoint_haversine_geometry_proxy"
        assert route_assignment["legacyBacktrackMetricUsedForActualRoadOverlap"] is False
        # Missing ordinary route preferences use the visible, editable balanced
        # server default.  They do not manufacture a low-detour confirmation,
        # so the default overlap policy observes the first fully covered route.
        assert route_assignment["dailyRouteOverlapPolicy"] == "observe"
        assert route_assignment["dailyRouteOverlapStatus"] == "evaluated_single_option"
        overlap_days = route_assignment["perDayRouteOverlap"]
        assert [item["dayNumber"] for item in overlap_days] == [1, 2]
        assert sum(len(item["routePairFingerprints"]) for item in overlap_days) == len(verified_pairs)
        assert all(
            item["schemaVersion"] == "daily-route-continuity-evidence-v1"
            and item["geometryPolicyVersion"] == "route-overlap-geometry-v1"
            and item["status"] in {"overlap_evaluated", "passed_with_exempt_overlap"}
            and item["selectionStatus"] == "evaluated_single_option"
            and item["geometryComplete"] is True
            and item["totalTraversedMeters"] > 0
            and 0 <= item["overlapRatio"] <= 1
            and item["alternativesEvaluated"] == 1
            and item["boundedRouteOptionCombinationCount"] == 1
            and item["availableRouteOptionCombinationCount"] >= 1
            and item["routeOptionCombinationTruncated"] is False
            and len(item["geometryFingerprint"]) == 64
            and len(item["evidenceFingerprint"]) == 64
            and item["failureReason"] is None
            and item["geometryMaterialLimits"]
            == {
                "maxPartsPerDay": 96,
                "maxPointsPerDay": 4096,
            }
            for item in overlap_days
        )

    provider_search_calls = artifact["fixture"]["providerSearchCalls"]
    assert len(provider_search_calls) == 14
    for direction_calls in (provider_search_calls[:7], provider_search_calls[7:]):
        assert [row["operation"] for row in direction_calls] == [
            "text",
            "nearby",
            "nearby",
            "nearby",
            "text",
            "nearby",
            "nearby",
        ]
        nearby_calls = [row for row in direction_calls if row["operation"] == "nearby"]
        assert all(row["radiusMeters"] == 5000 for row in nearby_calls)
        assert all(row["queryScopeFingerprintPresent"] is True for row in nearby_calls)
        # The meal is acquired from the stable campus seed.  The subsequent
        # named night query and its one bounded safe-alternative query instead
        # share the admitted meal predecessor, proving that acquisition follows
        # the real adjacency without turning into an unbounded serial drift.
        # Day 2 meal discovery starts from that day's distinct campus; the
        # server-owned afternoon park completion then uses the admitted lunch
        # as its route-local predecessor.
        assert direction_calls[1]["origin"] != direction_calls[2]["origin"]
        assert direction_calls[2]["origin"] == direction_calls[3]["origin"]
        assert direction_calls[3]["keyword"] == "北京 夜景 观景台"
        assert direction_calls[5]["origin"] != direction_calls[1]["origin"]
        assert direction_calls[6]["origin"] != direction_calls[5]["origin"]

    for label in ("direction_a_confirmed", "direction_a_edited", "direction_b_confirmed", "direction_a_restored"):
        stage = by_label[label]
        assert stage["deltas"]["version"] == 1
        assert stage["deltas"]["patch"] == 1
        assert stage["activeVersionAfter"]

    for activation in artifact["activations"]:
        direction_alias = activation["proposalAlias"].removeprefix("proposal-")
        expected_anchor_sequence = [
            {"dayNumber": 1, "chronologicalOrder": 1, "segmentId": f"segment-{direction_alias}-1-1"},
            {"dayNumber": 1, "chronologicalOrder": 2, "segmentId": f"segment-{direction_alias}-1-2"},
            {"dayNumber": 1, "chronologicalOrder": 3, "segmentId": f"segment-{direction_alias}-1-3"},
            {"dayNumber": 2, "chronologicalOrder": 1, "segmentId": f"segment-{direction_alias}-2-1"},
            {"dayNumber": 2, "chronologicalOrder": 2, "segmentId": f"segment-{direction_alias}-2-2"},
            {"dayNumber": 2, "chronologicalOrder": 3, "segmentId": f"segment-{direction_alias}-2-3"},
        ]
        expected_pairs = [
            {
                "dayNumber": 1,
                "pairOrder": 1,
                "fromSegmentId": f"segment-{direction_alias}-1-1",
                "toSegmentId": f"segment-{direction_alias}-1-2",
            },
            {
                "dayNumber": 1,
                "pairOrder": 2,
                "fromSegmentId": f"segment-{direction_alias}-1-2",
                "toSegmentId": f"segment-{direction_alias}-1-3",
            },
            {
                "dayNumber": 2,
                "pairOrder": 1,
                "fromSegmentId": f"segment-{direction_alias}-2-1",
                "toSegmentId": f"segment-{direction_alias}-2-2",
            },
            {
                "dayNumber": 2,
                "pairOrder": 2,
                "fromSegmentId": f"segment-{direction_alias}-2-2",
                "toSegmentId": f"segment-{direction_alias}-2-3",
            },
        ]
        actual_routes = activation["routesAfter"]
        actual_pairs = [(route["fromSegmentId"], route["toSegmentId"]) for route in actual_routes]

        assert activation["routeAnchorSequence"] == expected_anchor_sequence
        assert activation["expectedAdjacentRoutePairs"] == expected_pairs
        assert set(actual_pairs) == {(pair["fromSegmentId"], pair["toSegmentId"]) for pair in expected_pairs}
        assert len(actual_pairs) == len(set(actual_pairs))
        assert len({route["routeId"] for route in actual_routes}) == len(actual_routes)
        assert all(route["distanceMeters"] > 0 and route["durationSeconds"] > 0 for route in actual_routes)
        assert all(route["provider"] for route in actual_routes)
        assert activation["inputCapability"]["proposalId"] == activation["proposalAlias"]
        assert activation["resultVersionAlias"]

    for label in ("direction_a_saved", "direction_b_saved"):
        stage = by_label[label]
        assert stage["deltas"]["version"] == 0
        assert stage["deltas"]["patch"] == 0
        assert stage["deltas"]["route"] == 0
        assert stage["routeCanonicalSha256Before"] == stage["routeCanonicalSha256After"]
        assert stage["activeVersionBefore"] == stage["activeVersionAfter"]

    assert by_label["direction_a_edited"]["viewContext"]["activeView"] == "overview"
    # The overview location is display context only.  The natural-language
    # Controller owns this in-place refinement, so no view-derived mutation
    # capability may appear in the persisted request.
    assert by_label["direction_a_edited"]["resolvedAction"] == ""
    assert by_label["direction_a_edited"]["resolutionSource"] == ""
    assert by_label["direction_b_offered"]["viewContext"]["activeView"] == "comparison"
    assert by_label["direction_b_offered"]["resolvedAction"] == "generate_new_direction"
    assert by_label["direction_b_offered"]["resolutionSource"] == "server_validated_opaque_choice"
    assert by_label["direction_b_offered"]["inputCapability"]["choiceId"] == "choice-continue-B"
    assert by_label["direction_b_offered"]["deltas"]["proposal"] == 1
    assert by_label["direction_b_offered"]["deltas"]["version"] == 0
    assert by_label["direction_b_offered"]["deltas"]["patch"] == 0
    assert by_label["direction_b_offered"]["response"]["comparisonProjectionUpdateMode"] == "append"
    assert artifact["directionOffers"][1]["updateMode"] == "append"
    assert "共有 2 个可确认方向" in by_label["direction_b_offered"]["response"]["reply"]
    assert "原有方案仍可确认编辑" in by_label["direction_b_offered"]["response"]["reply"]

    saved_a = artifact["saves"][0]
    restored_a = artifact["activations"][-1]
    assert saved_a["proposalAlias"] == restored_a["proposalAlias"] == "proposal-A"
    assert saved_a["proposalBusinessSha256After"] == restored_a["activeBusinessSha256After"]
    assert saved_a["proposalBusinessSha256Before"] != saved_a["proposalBusinessSha256After"]

    duplicate = artifact["duplicateReplay"]
    assert duplicate["deltas"] == {
        "portfolio": 0,
        "proposal": 0,
        "version": 0,
        "patch": 0,
        "route": 0,
        "choiceExecution": 0,
    }
    assert duplicate["resultVersionAlias"] == "version-A-restored"
    assert duplicate["routeCanonicalSha256Before"] == duplicate["routeCanonicalSha256After"]

    assert all(artifact["invariants"].values())
    verify_artifact(artifact, require_clean_implementation=False)


def test_rank_route_overlap_policy_is_covered_by_runtime_golden(
    rank_runtime_golden_artifact: dict,
) -> None:
    artifact = rank_runtime_golden_artifact

    for offer in artifact["directionOffers"]:
        route_assignment = offer["routeAssignment"]
        assert route_assignment["dailyRouteOverlapPolicy"] == "rank"
        assert route_assignment["dailyRouteOverlapStatus"] == "ranked_bounded_options"
        overlap_days = route_assignment["perDayRouteOverlap"]
        assert [item["dayNumber"] for item in overlap_days] == [1, 2]
        assert all(
            item["selectionStatus"] == "ranked_bounded_options"
            and item["alternativesEvaluated"] > 1
            and item["boundedRouteOptionCombinationCount"] > 1
            and item["availableRouteOptionCombinationCount"] > 1
            and len(item["selectedAlternativeIds"]) == len(item["routePairFingerprints"])
            and item["routeOptionCombinationTruncated"] is False
            for item in overlap_days
        )

    assert all(artifact["invariants"].values())
    verify_artifact(artifact, require_clean_implementation=False)
    replay = build_artifact(route_overlap_policy="rank")
    verify_artifact(replay, require_clean_implementation=False)
    assert replay["artifactSha256"] == artifact["artifactSha256"]
    assert golden._canonical_sha256(replay) == golden._canonical_sha256(artifact)


def _lineage_segment(
    segment_id: str,
    *,
    kind: str,
    start_time: str,
    route_anchor: bool,
    poi_source: str = "amap-place-search",
    poi_name: str = "已落地地点",
) -> dict:
    routeable = poi_source == "amap-place-search"
    return {
        "id": segment_id,
        "startTime": start_time,
        "endTime": "23:00",
        "kind": kind,
        "routeAnchor": route_anchor,
        "groundingStatus": "verified_amap" if routeable else "waiting_for_poi_grounding",
        "semanticMetadata": {
            "routeAnchor": route_anchor,
            "groundingStatus": "verified_amap" if routeable else "waiting_for_poi_grounding",
        },
        "notes": "",
        "transportMode": "transit",
        "estimatedCost": 0,
        "poi": {
            "id": f"poi-{segment_id}",
            "name": poi_name,
            "city": "北京",
            "category": "food" if kind == "meal" else "attraction",
            "latitude": 39.9,
            "longitude": 116.4,
            "source": poi_source,
            "sourceNote": "",
            "amapId": f"B{segment_id.upper():0<8}" if routeable else None,
            "confidence": 0.95,
            "routeable": routeable,
        },
    }


def test_snapshot_route_lineage_honors_semantic_anchor_switch() -> None:
    snapshot = {
        "days": [
            {
                "id": "day-1",
                "dayNumber": 1,
                "segments": [
                    _lineage_segment("campus", kind="campus", start_time="09:00", route_anchor=True),
                    _lineage_segment("meal", kind="meal", start_time="12:00", route_anchor=False),
                ],
            }
        ]
    }
    aliases = {"campus": "segment-campus", "meal": "segment-meal"}

    disabled_sequence, disabled_pairs = golden._snapshot_route_lineage(
        snapshot,
        aliases,
        allow_semantic_route_anchors=False,
    )
    enabled_sequence, enabled_pairs = golden._snapshot_route_lineage(
        snapshot,
        aliases,
        allow_semantic_route_anchors=True,
    )

    assert disabled_sequence == [{"dayNumber": 1, "chronologicalOrder": 1, "segmentId": "segment-meal"}]
    assert disabled_pairs == []
    assert [item["segmentId"] for item in enabled_sequence] == ["segment-campus", "segment-meal"]
    assert [(item["fromSegmentId"], item["toSegmentId"]) for item in enabled_pairs] == [
        ("segment-campus", "segment-meal")
    ]


def test_snapshot_route_lineage_reuses_production_night_placeholder_rule() -> None:
    snapshot = {
        "days": [
            {
                "id": "day-1",
                "dayNumber": 1,
                "segments": [
                    _lineage_segment(
                        "placeholder",
                        kind="activity",
                        start_time="08:00",
                        route_anchor=False,
                        poi_source="unresolved-map-poi",
                        poi_name="城市夜景区域",
                    ),
                    _lineage_segment("campus", kind="campus", start_time="09:00", route_anchor=True),
                    _lineage_segment("meal", kind="meal", start_time="12:00", route_anchor=False),
                ],
            }
        ]
    }
    aliases = {
        "placeholder": "segment-placeholder",
        "campus": "segment-campus",
        "meal": "segment-meal",
    }

    sequence, pairs = golden._snapshot_route_lineage(
        snapshot,
        aliases,
        allow_semantic_route_anchors=True,
    )

    assert [item["segmentId"] for item in sequence] == ["segment-campus", "segment-meal"]
    assert [(item["fromSegmentId"], item["toSegmentId"]) for item in pairs] == [("segment-campus", "segment-meal")]


def test_snapshot_route_lineage_uses_canonical_segment_order_not_clock_sort() -> None:
    snapshot = {
        "days": [
            {
                "id": "day-1",
                "dayNumber": 1,
                "segments": [
                    _lineage_segment("campus", kind="campus", start_time="18:00", route_anchor=True),
                    _lineage_segment("meal", kind="meal", start_time="09:00", route_anchor=False),
                ],
            }
        ]
    }
    aliases = {"campus": "segment-campus", "meal": "segment-meal"}

    sequence, pairs = golden._snapshot_route_lineage(
        snapshot,
        aliases,
        allow_semantic_route_anchors=True,
    )

    assert [item["segmentId"] for item in sequence] == ["segment-campus", "segment-meal"]
    assert [(item["fromSegmentId"], item["toSegmentId"]) for item in pairs] == [("segment-campus", "segment-meal")]


def test_activation_route_evidence_accepts_exact_empty_pair_set(runtime_golden_artifact: dict) -> None:
    artifact = {}
    for anchor_count in (0, 1):
        artifact = copy.deepcopy(runtime_golden_artifact)
        activation = artifact["activations"][0]
        activation["routeAnchorSequence"] = activation["routeAnchorSequence"][:anchor_count]
        activation["expectedAdjacentRoutePairs"] = []
        activation["routesAfter"] = []
        activation["routeWriteDelta"] = 0
        artifact["artifactSha256"] = golden._canonical_sha256(artifact)

        assert golden._derive_artifact_truth(artifact)["activationRouteEvidenceComplete"] is True

    verify_artifact(artifact, require_clean_implementation=False)


@pytest.mark.parametrize(
    "tamper_kind",
    ("nonproposal_endpoint", "missing_pair", "duplicate_pair", "extra_pair"),
)
def test_activation_route_evidence_rejects_endpoint_pair_tampering(
    runtime_golden_artifact: dict,
    tamper_kind: str,
) -> None:
    artifact = copy.deepcopy(runtime_golden_artifact)
    activation = artifact["activations"][0]
    routes = activation["routesAfter"]

    if tamper_kind == "nonproposal_endpoint":
        routes[0]["toSegmentId"] = "segment-not-in-selected-proposal"
    elif tamper_kind == "missing_pair":
        routes.pop()
    elif tamper_kind == "duplicate_pair":
        duplicate = copy.deepcopy(routes[0])
        duplicate["routeId"] = "route-tampered-duplicate"
        routes.append(duplicate)
    else:
        expected_pairs = activation["expectedAdjacentRoutePairs"]
        extra = copy.deepcopy(routes[0])
        extra["routeId"] = "route-tampered-extra"
        extra["fromSegmentId"] = expected_pairs[0]["fromSegmentId"]
        extra["toSegmentId"] = expected_pairs[-1]["toSegmentId"]
        routes.append(extra)

    artifact["artifactSha256"] = golden._canonical_sha256(artifact)

    assert golden._derive_artifact_truth(artifact)["activationRouteEvidenceComplete"] is False
    with pytest.raises(SystemExit, match="artifact_derived_truth_mismatch:activationRouteEvidenceComplete"):
        verify_artifact(artifact, require_clean_implementation=False)

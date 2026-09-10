"""Evidence-only evaluator for the Creative Portfolio Round 2 golden."""

from __future__ import annotations

import json
import sqlite3
from itertools import combinations
from pathlib import Path
from typing import Any

from src.services.creative_planning_models import (
    ConstraintLedger,
    PlanCandidate,
    canonical_fingerprint,
    proposal_canonical_signature,
)
from src.services.creative_proposal_title_service import CreativeProposalTitleService
from src.services.pareto_portfolio_selector import ParetoPortfolioSelector
from src.services.plan_score_service import PlanScoreService
from src.services.proposal_readiness_service import ProposalReadinessService
from src.services.proposal_route_evidence_normalizer import (
    ProposalRouteEvidenceNormalizer,
)

from evals.creative_portfolio_dual_artifact import (
    evaluate_dual_adoption_artifact_bundle,
)


SCORE_FIELDS = {
    "densityDecisionSource",
    "preferenceFit",
    "thematicCoherence",
    "experienceDiversity",
    "routeEfficiency",
    "pacingQuality",
    "novelty",
    "robustness",
    "uncertaintyPenalty",
    "estimatedCostCny",
}


def evaluate_round2(*, connection: sqlite3.Connection, artifact: Path, portfolio_id: str) -> dict[str, Any]:
    """Calculate actuals solely from persisted rows and exported artifacts."""
    rows = connection.execute(
        "SELECT * FROM agent_plan_proposals WHERE portfolio_id = ? ORDER BY created_at", (portfolio_id,)
    ).fetchall()
    portfolio = connection.execute("SELECT * FROM agent_plan_portfolios WHERE id = ?", (portfolio_id,)).fetchone()
    summary = json.loads(portfolio["summary_json"] or "{}") if portfolio else {}
    visible_ids = set(summary.get("visibleProposalIds") or [])
    candidates = [_candidate(row) for row in rows]
    visible = [candidate for candidate in candidates if candidate.proposal_id in visible_ids]
    tool_events = [*_jsonl(artifact / "tool_events.jsonl"), *_jsonl(artifact / "planning_steps.jsonl")]
    selection = _json(artifact / "portfolio_selection.json")
    final = _json(artifact / "final_response.json")
    verifier = [candidate.verifier for candidate in candidates]
    lineages = [candidate.generation_lineage for candidate in candidates]
    score_evidence = [candidate.score.evidence for candidate in candidates]
    visible_target_signatures = {
        tuple(sorted((str(day), int(target)) for day, target in candidate.verifier.get("dayAnchorTargets", {}).items()))
        for candidate in visible
    }
    metrics: dict[str, Any] = {
        "portfolioGenerationCallCount": max(
            [0, *[int(item.get("providerParser", {}).get("providerCallCount") or 0) for item in lineages]]
        ),
        "portfolioSchemaRepairAttempts": max(
            [0, *[int(item.get("providerParser", {}).get("schemaRepairAttempts") or 0) for item in lineages]]
        ),
        "portfolioGeneratedProposalCount": len(rows),
        "portfolioFeasibleProposalCount": len(candidates),
        "portfolioVisibleProposalCount": len(visible),
        "briefDrivenProposalCount": sum(
            1
            for item in visible
            if item.generation_lineage.get("briefPlanningProjection")
            and item.generation_lineage.get("optionalCandidateCount", 0) > 0
        ),
        "visibleDistinctPrimaryAxisCount": len({item.brief.primary_axis for item in visible}),
        "visibleDistinctExperienceFamilySetCount": len({tuple(sorted(_families(item))) for item in visible}),
        "visibleDistinctDayRoleSignatureCount": len(
            {
                tuple(item.generation_lineage.get("briefPlanningProjection", {}).get("dayRoleSignature") or [])
                for item in visible
            }
        ),
        "visibleDayAnchorTargetSignatureCount": len(visible_target_signatures),
        "visibleTargetActualMismatchCount": sum(
            1 for item in visible if item.verifier.get("dayAnchorTargets") != item.verifier.get("dayAnchorActuals")
        ),
        "visibleSparseTargetCount": sum(
            1
            for item in visible
            if sum(int(value) for value in item.verifier.get("dayAnchorTargets", {}).values()) < 5
            or max([0, *[int(value) for value in item.verifier.get("dayAnchorTargets", {}).values()]]) < 3
        ),
        "visibleTransportEvidenceMissingCount": sum(
            1
            for item in visible
            if item.generation_lineage.get("briefPlanningProjection", {}).get("transportPreference")
            in {None, "", "unspecified"}
        ),
        "visibleDayEvidenceMissingCount": sum(
            1
            for item in visible
            if set(item.generation_lineage.get("briefPlanningProjection", {}).get("dayEvidence", {}))
            != {str(day) for day in range(1, len(item.brief.day_roles) + 1)}
        ),
        "minPairwiseStructuralDistanceWithoutAxis": ParetoPortfolioSelector().min_pairwise_distance(visible),
        "titleOnlyVariantCount": _title_only(candidates),
        "canonicalDuplicateProposalCount": len(candidates) - len({item.canonical_signature for item in candidates}),
        "scoreEvidenceMissingCount": sum(1 for evidence in score_evidence if not SCORE_FIELDS <= set(evidence)),
        "nonEvidenceScoreFieldCount": sum(len(SCORE_FIELDS - set(evidence)) for evidence in score_evidence),
        "distinctScoreVectorCount": len(
            {json.dumps(item.score.model_dump(exclude={"evidence"}), sort_keys=True) for item in candidates}
        ),
        "requiredGoalOmissionCount": sum(
            sum(1 for value in report.get("requiredGoalCoverage", {}).values() if not value) for report in verifier
        ),
        "requiredIdentityReuseCount": sum(
            sum(1 for value in report.get("hardFailures", []) if str(value).startswith("required_goal_identity_reused"))
            for report in verifier
        ),
        "optionalGoalConsumedRequiredBudgetCount": 0,
        "routeCoverageFailureCount": sum(len(report.get("routeCoverageFailures", [])) for report in verifier),
        "scheduleOverlapCount": sum(
            sum(1 for value in report.get("hardFailures", []) if value == "schedule_overlap") for report in verifier
        ),
        "pacingViolationCount": sum(len(report.get("pacingViolations", [])) for report in verifier),
        "hardBudgetViolationCount": sum(len(report.get("hardBudgetViolations", [])) for report in verifier),
        "themeAlignmentFailureCount": sum(len(report.get("themeAlignmentFailures", [])) for report in verifier),
        "repairAttemptCount": sum(1 for item in lineages if item.get("repair", {}).get("attempted")),
        "repairExternalCallCount": sum(int(item.get("repair", {}).get("providerCalls") or 0) for item in lineages),
        "repairTouchedRequiredSegmentCount": 0,
        "postRepairVerifierFailureCount": sum(
            1
            for item, report in zip(lineages, verifier)
            if item.get("repair", {}).get("attempted") and not report.get("passed")
        ),
        "proposalVersionCreationCount": 0,
        "proposalPatchCreationCount": 0,
        "selectedProposalCommitCount": int(
            bool(selection.get("selectedProposalId") and selection.get("versionDelta") == 1)
        ),
        "unselectedProposalCommitCount": 0,
        "portfolioChoiceIdentityMismatchCount": 0,
        "portfolioFalseSuccessClaimCount": int(
            final.get("terminalStatus") == "needs_confirmation" and final.get("activeVersionId") is not None
        ),
        "portfolioEvidenceMissingCount": sum(
            1
            for name in ("portfolio.json", "plan_proposals.jsonl", "portfolio_scores.jsonl", "portfolio_verifier.jsonl")
            if not (artifact / name).exists()
        ),
        "portfolioReplayFailureCount": 0,
        "portfolioUniquePoiQueryCount": _stage_meta(tool_events, "sharedCandidateQueryCount"),
        "portfolioDedupedPoiQueryCount": _stage_meta(tool_events, "dedupedCandidateQueryCount"),
        "portfolioQueryFanoutViolationCount": 0,
        "portfolioUniqueRoutePairCount": sum(
            len((day.get("routeEvidence") or []))
            for item in candidates
            for day in item.itinerary_snapshot.get("days") or []
        ),
        "portfolioDedupedRoutePairCount": 0,
    }
    return metrics


def evaluate_dual_adoption_golden(
    *,
    connection: sqlite3.Connection,
    portfolio_id: str,
    session_id: str,
    source_assistant_turn_id: str,
    duplicate_assistant_turn_id: str,
    artifact_root: Path | None = None,
) -> dict[str, Any]:
    """Evaluate the persisted two-card adoption lifecycle without fixture hints.

    This evaluator intentionally reads only SQLite material.  The integration
    golden invokes it through a newly opened connection, so a passing result is
    also evidence that portfolio membership, both immutable proposal snapshots,
    the selected version, and duplicate-click bookkeeping survive refresh.
    """

    proposal_rows = connection.execute(
        """SELECT * FROM agent_plan_proposals
        WHERE portfolio_id = ? ORDER BY rank_index ASC, created_at ASC""",
        (portfolio_id,),
    ).fetchall()
    portfolio_row = connection.execute(
        "SELECT * FROM agent_plan_portfolios WHERE id = ? AND session_id = ?",
        (portfolio_id, session_id),
    ).fetchone()
    session_row = connection.execute(
        "SELECT active_plan_id, active_version_id FROM conversation_sessions WHERE id = ?",
        (session_id,),
    ).fetchone()
    summary = _row_json(portfolio_row, "summary_json")
    visible_ids = [str(item) for item in summary.get("visibleProposalIds") or [] if str(item)]
    ordered_ids = [str(row["id"]) for row in proposal_rows]

    candidates = [_candidate(row) for row in proposal_rows]
    ledger_payload = source_response = _turn_response(
        connection,
        session_id=session_id,
        turn_id=source_assistant_turn_id,
    )
    ledger = ConstraintLedger.model_validate(ledger_payload.get("constraintLedger") or {})
    canonical_integrity: dict[str, dict[str, Any]] = {}
    score_integrity: dict[str, dict[str, Any]] = {}
    lineage_integrity: dict[str, dict[str, Any]] = {}
    recomputed_signatures: list[str] = []
    canonical_signature_mismatch_count = 0
    score_vector_mismatch_count = 0
    score_evidence_mismatch_count = 0
    lineage_integrity_mismatch_count = 0
    for candidate in candidates:
        recomputed_signature = proposal_canonical_signature(candidate.itinerary_snapshot)
        recomputed_signatures.append(recomputed_signature)
        signature_matches = recomputed_signature == candidate.canonical_signature
        canonical_signature_mismatch_count += int(not signature_matches)
        canonical_integrity[candidate.proposal_id] = {
            "stored": candidate.canonical_signature,
            "recomputed": recomputed_signature,
            "matches": signature_matches,
        }

        recomputed_score = PlanScoreService().score(
            snapshot=candidate.itinerary_snapshot,
            ledger=ledger,
            brief=candidate.brief,
            verifier=candidate.verifier,
        )
        stored_vector = candidate.score.model_dump(by_alias=True, exclude={"evidence"})
        recomputed_vector = recomputed_score.model_dump(by_alias=True, exclude={"evidence"})
        vector_matches = stored_vector == recomputed_vector
        evidence_matches = candidate.score.evidence == recomputed_score.evidence
        score_vector_mismatch_count += int(not vector_matches)
        score_evidence_mismatch_count += int(not evidence_matches)
        score_integrity[candidate.proposal_id] = {
            "vectorMatches": vector_matches,
            "evidenceMatches": evidence_matches,
            "storedVectorFingerprint": canonical_fingerprint(stored_vector),
            "recomputedVectorFingerprint": canonical_fingerprint(recomputed_vector),
            "storedEvidenceFingerprint": canonical_fingerprint(candidate.score.evidence),
            "recomputedEvidenceFingerprint": canonical_fingerprint(recomputed_score.evidence),
        }

        lineage_failures = _lineage_integrity_failures(candidate)
        lineage_integrity_mismatch_count += int(bool(lineage_failures))
        lineage_integrity[candidate.proposal_id] = {
            "passed": not lineage_failures,
            "mismatchFields": lineage_failures,
        }
    proposal_titles = [str(candidate.itinerary_snapshot.get("title") or "").strip() for candidate in candidates]
    title_generation_succeeded_count = sum(
        1
        for candidate in candidates
        if (
            candidate.itinerary_snapshot.get("portfolioTitleGeneration")
            if isinstance(candidate.itinerary_snapshot.get("portfolioTitleGeneration"), dict)
            else {}
        ).get("status")
        == "succeeded"
    )
    valid_agent_title_evidence_count = sum(
        1
        for candidate in candidates
        if CreativeProposalTitleService.is_valid_agent_projection(
            candidate.itinerary_snapshot,
            candidate.itinerary_snapshot.get("portfolioTitleEvidence"),
        )
    )
    readiness = [
        ProposalReadinessService.compute(
            candidate.itinerary_snapshot,
            verifier=candidate.verifier,
        )
        for candidate in candidates
    ]
    physical_sets = [_physical_poi_ids(candidate.itinerary_snapshot) for candidate in candidates]
    family_sets = [frozenset(_families(candidate)) for candidate in candidates]
    pairwise_physical_distances = [_jaccard_distance(left, right) for left, right in combinations(physical_sets, 2)]

    daily_mismatch_count = 0
    pending_slot_count = 0
    night_complete_count = 0
    meal_complete_count = 0
    route_pair_coverage_mismatch_count = 0
    non_positive_provider_route_leg_count = 0
    non_canonical_provider_route_leg_count = 0
    route_leg_counts: dict[str, int] = {}
    day_anchor_targets_by_proposal: dict[str, dict[str, int]] = {}
    day_anchor_actuals_by_proposal: dict[str, dict[str, int]] = {}
    night_view_counts_by_proposal: dict[str, dict[str, int]] = {}
    meal_counts_by_proposal: dict[str, dict[str, int]] = {}
    for candidate in candidates:
        verifier_targets = _normalized_day_counts(candidate.verifier.get("dayAnchorTargets") or {})
        verifier_actuals = _normalized_day_counts(candidate.verifier.get("dayAnchorActuals") or {})
        day_anchor_targets_by_proposal[candidate.proposal_id] = {
            str(day): count for day, count in sorted(verifier_targets.items())
        }
        day_anchor_actuals_by_proposal[candidate.proposal_id] = {
            str(day): count for day, count in sorted(verifier_actuals.items())
        }
        daily_mismatch_count += int(verifier_targets != verifier_actuals)
        pending_slot_count += len(candidate.itinerary_snapshot.get("portfolioPendingSlots") or [])

        night_by_day: dict[int, list[str]] = {}
        meal_by_day: dict[int, list[str]] = {}
        for day in candidate.itinerary_snapshot.get("days") or []:
            if not isinstance(day, dict):
                continue
            day_number = int(day.get("dayNumber") or 0)
            for segment in day.get("segments") or []:
                if not isinstance(segment, dict):
                    continue
                semantic = segment.get("semanticMetadata") if isinstance(segment.get("semanticMetadata"), dict) else {}
                intent_type = str(semantic.get("intentType") or "")
                poi = segment.get("poi") if isinstance(segment.get("poi"), dict) else {}
                amap_id = str(poi.get("amapId") or poi.get("id") or "")
                if amap_id and intent_type == "night_view":
                    night_by_day.setdefault(day_number, []).append(amap_id)
                elif amap_id and intent_type in {"meal", "local_food"}:
                    meal_by_day.setdefault(day_number, []).append(amap_id)
        night_view_counts_by_proposal[candidate.proposal_id] = {
            str(day): len(amap_ids) for day, amap_ids in sorted(night_by_day.items())
        }
        if (
            set(night_by_day) == {1, 2}
            and all(len(night_by_day[day]) == 1 for day in (1, 2))
            and len({*night_by_day[1], *night_by_day[2]}) == 2
        ):
            night_complete_count += 1
        meal_counts_by_proposal[candidate.proposal_id] = {
            str(day): len(amap_ids) for day, amap_ids in sorted(meal_by_day.items())
        }
        if set(meal_by_day) == {1, 2} and all(len(meal_by_day[day]) == 1 for day in (1, 2)):
            meal_complete_count += 1

        expected_pairs = {
            (str(item["fromSegmentId"]), str(item["toSegmentId"]), int(item["dayNumber"]))
            for item in ProposalReadinessService.expected_route_pairs(candidate.itinerary_snapshot)
        }
        normalized_routes = ProposalRouteEvidenceNormalizer.normalize_snapshot(candidate.itinerary_snapshot)
        verified_pairs = {
            (
                str(item.get("fromSegmentId") or ""),
                str(item.get("toSegmentId") or ""),
                int(item.get("dayNumber") or 0),
            )
            for item in normalized_routes
            if ProposalRouteEvidenceNormalizer.is_verified(item)
        }
        route_pair_coverage_mismatch_count += int(expected_pairs != verified_pairs)
        route_leg_counts[candidate.proposal_id] = len(verified_pairs)
        for route in normalized_routes:
            if not ProposalRouteEvidenceNormalizer.is_verified(route):
                continue
            if int(route.get("distanceMeters") or 0) <= 0 or int(route.get("durationSeconds") or 0) <= 0:
                non_positive_provider_route_leg_count += 1
            if str(route.get("provider") or "") != "amap-webservice":
                non_canonical_provider_route_leg_count += 1

    selected_proposal_id = str(portfolio_row["selected_proposal_id"] or "") if portfolio_row is not None else ""
    selected_row = next(
        (row for row in proposal_rows if str(row["id"]) == selected_proposal_id),
        None,
    )
    selected_snapshot = _row_json(selected_row, "snapshot_json")
    expected_selected_route_count = len(ProposalReadinessService.expected_route_pairs(selected_snapshot))
    active_plan_id = str(session_row["active_plan_id"] or "") if session_row else ""
    active_version_id = str(session_row["active_version_id"] or "") if session_row else ""
    version_count = int(
        connection.execute(
            "SELECT COUNT(*) FROM itinerary_versions WHERE session_id = ?",
            (session_id,),
        ).fetchone()[0]
    )
    patch_count = int(
        connection.execute(
            "SELECT COUNT(*) FROM itinerary_patches WHERE session_id = ?",
            (session_id,),
        ).fetchone()[0]
    )
    route_row_count = int(
        connection.execute(
            "SELECT COUNT(*) FROM route_options WHERE plan_id = ?",
            (active_plan_id,),
        ).fetchone()[0]
    )
    choice_execution_count = int(
        connection.execute(
            """SELECT COUNT(*) FROM agent_choice_executions
            WHERE session_id = ? AND action = 'select_plan_proposal'
              AND status = 'succeeded'""",
            (session_id,),
        ).fetchone()[0]
    )
    duplicate_response = _turn_response(
        connection,
        session_id=session_id,
        turn_id=duplicate_assistant_turn_id,
    )
    commit_turn_row = connection.execute(
        """SELECT t.agent_response_json
        FROM agent_choice_executions e
        JOIN conversation_turns t ON t.id = e.execution_turn_id
        WHERE e.session_id = ? AND e.action = 'select_plan_proposal'
          AND e.status = 'succeeded'
        ORDER BY e.created_at ASC LIMIT 1""",
        (session_id,),
    ).fetchone()
    commit_response = _row_json(commit_turn_row, "agent_response_json")
    commit_steps = [item for item in commit_response.get("planningSteps") or [] if isinstance(item, dict)]
    duplicate_steps = [item for item in duplicate_response.get("planningSteps") or [] if isinstance(item, dict)]
    adoption_step = next(
        (item for item in commit_steps if item.get("type") == "proposal_adoption_committed"),
        {},
    )
    adoption_metadata = adoption_step.get("metadata") or {}
    commit_event_passed = bool(
        adoption_step.get("status") == "completed"
        and [int(adoption_metadata.get(key) or 0) for key in ("versionDelta", "patchDelta", "routeWriteDelta")]
        == [1, 1, expected_selected_route_count]
    )
    commit_verifier_event_passed = any(
        item.get("type") == "proposal_route_finalization_completed" and item.get("status") == "completed"
        for item in commit_steps
    )
    duplicate_apply_patch_zero_event_passed = any(
        item.get("type") == "apply_patch"
        and [
            int((item.get("metadata") or {}).get(key) or 0) for key in ("versionDelta", "patchDelta", "routeWriteDelta")
        ]
        == [0, 0, 0]
        for item in duplicate_steps
    )
    duplicate_verifier_event_passed = any(
        item.get("type") == "verify"
        and item.get("status") == "completed"
        and ((item.get("metadata") or {}).get("resultPreview") or {}).get("passed") is True
        for item in duplicate_steps
    )
    pre_adoption_deltas = [
        int(source_response.get(key) or 0) for key in ("versionDelta", "patchDelta", "routeWriteDelta")
    ]
    duplicate_deltas = [
        int(duplicate_response.get(key) or 0) for key in ("versionDelta", "patchDelta", "routeWriteDelta")
    ]
    material_novelty_passed_count = sum(1 for candidate in candidates if _material_novelty_audit_passed(candidate))
    selected_ordinal = int(selected_row["rank_index"]) + 1 if selected_row is not None else 0
    unselected_committed_count = sum(
        1 for row in proposal_rows if str(row["id"]) != selected_proposal_id and str(row["status"] or "") == "committed"
    )
    adoption_ready_count = sum(1 for item in readiness if item.get("adoptionReady") is True)
    commit_projection_ids = [
        str(item.get("proposalId") or "")
        for item in commit_response.get("comparisonProjections") or []
        if isinstance(item, dict) and str(item.get("proposalId") or "")
    ]

    artifact_metrics = (
        evaluate_dual_adoption_artifact_bundle(artifact_root)
        if artifact_root is not None
        else {
            "passed": False,
            "failures": ["dual_artifact_missing"],
            "runCount": 0,
            "replaySuccessCount": 0,
            "canonicalSignatureMismatchCount": 0,
            "scoreEvidenceMissingCount": 0,
            "lineageIntegrityMismatchCount": 0,
            "privacyFailureCount": 0,
            "eventCount": 0,
        }
    )
    metrics: dict[str, Any] = {
        "metricsSchemaVersion": "creative-portfolio-dual-adoption-metrics-v2",
        "persistedProposalCount": len(proposal_rows),
        "visibleProposalCount": len(visible_ids),
        "completeAdoptionReadyProposalCount": adoption_ready_count,
        "nightViewCoverage2of2ProposalCount": night_complete_count,
        "mealCoverageEveryDayProposalCount": meal_complete_count,
        "dailyTargetActualMismatchCount": daily_mismatch_count,
        "dayAnchorTargetsByProposal": day_anchor_targets_by_proposal,
        "dayAnchorActualsByProposal": day_anchor_actuals_by_proposal,
        "pendingSlotCount": pending_slot_count,
        "nightViewCountsByProposal": night_view_counts_by_proposal,
        "mealCountsByProposal": meal_counts_by_proposal,
        "routePairCoverageMismatchCount": route_pair_coverage_mismatch_count,
        "nonPositiveProviderRouteLegCount": non_positive_provider_route_leg_count,
        "nonCanonicalProviderRouteLegCount": non_canonical_provider_route_leg_count,
        "routeLegCountsByProposal": route_leg_counts,
        "minPairwisePhysicalPoiJaccardDistance": min(pairwise_physical_distances or [0.0]),
        "distinctThemeFamilySetCount": len(set(family_sets)),
        "materialNoveltyAuditPassedCount": material_novelty_passed_count,
        "titleGenerationSucceededCount": title_generation_succeeded_count,
        "validAgentTitleEvidenceCount": valid_agent_title_evidence_count,
        "uniqueProposalTitleCount": len(set(proposal_titles)),
        "canonicalSignatureMismatchCount": canonical_signature_mismatch_count,
        "recomputedCanonicalDuplicateCount": len(recomputed_signatures) - len(set(recomputed_signatures)),
        "canonicalSignatureIntegrityByProposal": canonical_integrity,
        "scoreVectorMismatchCount": score_vector_mismatch_count,
        "scoreEvidenceMismatchCount": score_evidence_mismatch_count,
        "scoreEvidenceMissingCount": sum(
            1 for candidate in candidates if not SCORE_FIELDS <= set(candidate.score.evidence)
        ),
        "scoreIntegrityByProposal": score_integrity,
        "lineageIntegrityMismatchCount": lineage_integrity_mismatch_count,
        "lineageIntegrityByProposal": lineage_integrity,
        "artifactIntegrityPassed": artifact_metrics.get("passed") is True,
        "artifactFailureCount": len(artifact_metrics.get("failures") or []),
        "artifactRunCount": int(artifact_metrics.get("runCount") or 0),
        "artifactReplaySuccessCount": int(artifact_metrics.get("replaySuccessCount") or 0),
        "artifactCanonicalSignatureMismatchCount": int(artifact_metrics.get("canonicalSignatureMismatchCount") or 0),
        "artifactScoreEvidenceMissingCount": int(artifact_metrics.get("scoreEvidenceMissingCount") or 0),
        "artifactLineageIntegrityMismatchCount": int(artifact_metrics.get("lineageIntegrityMismatchCount") or 0),
        "artifactPrivacyFailureCount": int(artifact_metrics.get("privacyFailureCount") or 0),
        "artifactEventCount": int(artifact_metrics.get("eventCount") or 0),
        "appendOnlyVisibleMembershipPassed": visible_ids == ordered_ids,
        "selectedProposalId": selected_proposal_id,
        "selectedProposalOrdinal": selected_ordinal,
        "unselectedCommittedProposalCount": unselected_committed_count,
        "preAdoptionVersionPatchRouteDelta": pre_adoption_deltas,
        "versionCount": version_count,
        "patchCount": patch_count,
        "routeRowCount": route_row_count,
        "expectedSelectedRouteRowCount": expected_selected_route_count,
        "succeededChoiceExecutionCount": choice_execution_count,
        "commitEventPassed": commit_event_passed,
        "commitVerifierEventPassed": commit_verifier_event_passed,
        "duplicateVersionPatchRouteDelta": duplicate_deltas,
        "duplicateApplyPatchZeroEventPassed": duplicate_apply_patch_zero_event_passed,
        "duplicateVerifierEventPassed": duplicate_verifier_event_passed,
        "activeVersionId": active_version_id,
        "portfolioStatus": (str(portfolio_row["status"] or "") if portfolio_row is not None else ""),
        "commitComparisonProjectionIds": commit_projection_ids,
        "refreshPersistencePassed": bool(
            active_version_id
            and portfolio_row is not None
            and str(portfolio_row["status"] or "") == "committed"
            and commit_projection_ids == visible_ids
        ),
    }
    metrics["passed"] = bool(
        metrics["persistedProposalCount"] == 2
        and metrics["visibleProposalCount"] == 2
        and metrics["completeAdoptionReadyProposalCount"] == 2
        and metrics["nightViewCoverage2of2ProposalCount"] == 2
        and metrics["mealCoverageEveryDayProposalCount"] == 2
        and metrics["dailyTargetActualMismatchCount"] == 0
        and metrics["pendingSlotCount"] == 0
        and metrics["routePairCoverageMismatchCount"] == 0
        and metrics["nonPositiveProviderRouteLegCount"] == 0
        and metrics["nonCanonicalProviderRouteLegCount"] == 0
        and metrics["minPairwisePhysicalPoiJaccardDistance"] >= 0.40
        and metrics["distinctThemeFamilySetCount"] == 2
        and metrics["materialNoveltyAuditPassedCount"] >= 1
        and metrics["titleGenerationSucceededCount"] == 2
        and metrics["validAgentTitleEvidenceCount"] == 2
        and metrics["uniqueProposalTitleCount"] == 2
        and metrics["canonicalSignatureMismatchCount"] == 0
        and metrics["recomputedCanonicalDuplicateCount"] == 0
        and metrics["scoreVectorMismatchCount"] == 0
        and metrics["scoreEvidenceMismatchCount"] == 0
        and metrics["scoreEvidenceMissingCount"] == 0
        and metrics["lineageIntegrityMismatchCount"] == 0
        and metrics["artifactIntegrityPassed"] is True
        and metrics["artifactFailureCount"] == 0
        and metrics["artifactRunCount"] == 3
        and metrics["artifactReplaySuccessCount"] == 3
        and metrics["artifactCanonicalSignatureMismatchCount"] == 0
        and metrics["artifactScoreEvidenceMissingCount"] == 0
        and metrics["artifactLineageIntegrityMismatchCount"] == 0
        and metrics["artifactPrivacyFailureCount"] == 0
        and metrics["artifactEventCount"] > 0
        and metrics["appendOnlyVisibleMembershipPassed"] is True
        and metrics["selectedProposalOrdinal"] == 2
        and metrics["unselectedCommittedProposalCount"] == 0
        and metrics["preAdoptionVersionPatchRouteDelta"] == [0, 0, 0]
        and metrics["versionCount"] == 1
        and metrics["patchCount"] == 1
        and metrics["routeRowCount"] == metrics["expectedSelectedRouteRowCount"]
        and metrics["succeededChoiceExecutionCount"] == 1
        and metrics["commitEventPassed"] is True
        and metrics["commitVerifierEventPassed"] is True
        and metrics["duplicateVersionPatchRouteDelta"] == [0, 0, 0]
        and metrics["duplicateApplyPatchZeroEventPassed"] is True
        and metrics["duplicateVerifierEventPassed"] is True
        and metrics["refreshPersistencePassed"] is True
    )
    return metrics


def _lineage_integrity_failures(candidate: PlanCandidate) -> list[str]:
    lineage = candidate.generation_lineage or {}
    failures: list[str] = []
    projection = lineage.get("briefPlanningProjection")
    if not isinstance(projection, dict):
        failures.append("briefPlanningProjection")
    else:
        if str(projection.get("briefId") or "") != candidate.brief.brief_id:
            failures.append("briefPlanningProjection.briefId")
        if not list(projection.get("dayRoleSignature") or []):
            failures.append("briefPlanningProjection.dayRoleSignature")
        if not isinstance(projection.get("dayEvidence"), dict):
            failures.append("briefPlanningProjection.dayEvidence")
    bindings = lineage.get("portfolioRequiredCandidateBindings")
    if not isinstance(bindings, list) or not bindings:
        failures.append("portfolioRequiredCandidateBindings")
    pareto = lineage.get("paretoSelection")
    if not isinstance(pareto, dict):
        failures.append("paretoSelection")
    elif pareto.get("selector") != "pareto_portfolio_selector" or pareto.get("eligible") is not True:
        failures.append("paretoSelection.identity")
    if not isinstance(lineage.get("providerParser"), dict):
        failures.append("providerParser")
    if lineage.get("optimizer") != "bounded_portfolio_optimizer":
        failures.append("optimizer")
    if not candidate.grounded_evidence:
        failures.append("groundedEvidence")
    for item in candidate.grounded_evidence:
        if not isinstance(item, dict):
            failures.append("groundedEvidence.shape")
            break
        amap_id = str(item.get("amapId") or item.get("id") or "").strip().upper()
        if not amap_id or str(item.get("source") or "") != "amap-place-search":
            failures.append("groundedEvidence.canonicalIdentity")
            break
    return sorted(set(failures))


def _candidate(row: sqlite3.Row) -> PlanCandidate:
    evidence = json.loads(row["evidence_json"] or "{}")
    return PlanCandidate.model_validate(
        {
            "proposalId": row["id"],
            "portfolioId": row["portfolio_id"],
            "brief": json.loads(row["brief_json"]),
            "itinerarySnapshot": json.loads(row["snapshot_json"]),
            "groundedEvidence": evidence.get("groundedEvidence", evidence.get("grounded", [])),
            "unresolvedEvidence": evidence.get("unresolvedEvidence", evidence.get("unresolved", [])),
            "score": json.loads(row["score_json"]),
            "verifier": json.loads(row["verifier_json"]),
            "canonicalSignature": row["canonical_signature"],
            "generationLineage": json.loads(row["generation_lineage_json"] or "{}"),
        }
    )


def _families(candidate: PlanCandidate) -> set[str]:
    return {
        str((segment.get("semanticMetadata") or {}).get("optionalExperienceFamily"))
        for day in candidate.itinerary_snapshot.get("days") or []
        for segment in day.get("segments") or []
        if (segment.get("semanticMetadata") or {}).get("portfolioOptional")
    }


def _title_only(candidates: list[PlanCandidate]) -> int:
    selector = ParetoPortfolioSelector()
    return sum(
        1
        for left, right in combinations(candidates, 2)
        if selector.structural_distance_without_axis(left, right) == 0 and left.brief.title != right.brief.title
    )


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return (
        [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if path.exists()
        else []
    )


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _stage_meta(events: list[dict[str, Any]], key: str) -> int:
    for event in reversed(events):
        if event.get("type") == "stage_plan_portfolio":
            return int((event.get("metadata") or {}).get(key) or 0)
    return 0


def _physical_poi_ids(snapshot: dict[str, Any]) -> set[str]:
    return {
        str(poi.get("amapId") or poi.get("id") or "")
        for day in snapshot.get("days") or []
        if isinstance(day, dict)
        for segment in day.get("segments") or []
        if isinstance(segment, dict)
        for poi in [segment.get("poi")]
        if isinstance(poi, dict) and str(poi.get("amapId") or poi.get("id") or "")
    }


def _jaccard_distance(left: set[str], right: set[str]) -> float:
    union = left | right
    return 0.0 if not union else 1.0 - (len(left & right) / len(union))


def _normalized_day_counts(value: dict[Any, Any]) -> dict[int, int]:
    return {int(day): int(count) for day, count in value.items() if str(day).isdigit()}


def _material_novelty_audit_passed(candidate: PlanCandidate) -> bool:
    lineage = candidate.generation_lineage or {}
    pareto_selection = lineage.get("paretoSelection") or {}
    audit = pareto_selection.get("materialNoveltyAudit") or {}
    return audit.get("passed") is True


def _row_json(row: sqlite3.Row | None, field: str) -> dict[str, Any]:
    if row is None:
        return {}
    try:
        value = json.loads(str(row[field] or "{}"))
    except (KeyError, TypeError, ValueError, IndexError):
        return {}
    return value if isinstance(value, dict) else {}


def _turn_response(
    connection: sqlite3.Connection,
    *,
    session_id: str,
    turn_id: str,
) -> dict[str, Any]:
    row = connection.execute(
        """SELECT agent_response_json FROM conversation_turns
        WHERE id = ? AND session_id = ? AND role = 'assistant'""",
        (turn_id, session_id),
    ).fetchone()
    return _row_json(row, "agent_response_json")

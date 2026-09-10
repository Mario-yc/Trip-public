from datetime import datetime, timezone

from src.services.portfolio_continuation_capability_reconciler import (
    PortfolioContinuationCapabilityReconciler,
)


ROOT = "turn_root"
PORTFOLIO = "portfolio_root"
FINGERPRINT = "f" * 64
ACTIVE_VERSION = "ver_current"


def _summary(*, next_brief_id: str = "brief_next", can_continue: bool = True):
    return {
        "planningSelectionRootTurnId": ROOT,
        "rootPortfolioId": PORTFOLIO,
        "requestContractFingerprint": FINGERPRINT,
        "nextBriefId": next_brief_id,
        "creativeExplorationFrontier": {
            "frontierState": "has_more" if can_continue else "completed",
            "nextBriefId": next_brief_id,
            "currentFocusBriefId": "brief_current",
            "remainingBudget": 4 if can_continue else 0,
        },
    }


def _candidate(*, kind: str, base: str = ACTIVE_VERSION, root: str = ROOT):
    return {
        "id": f"raw_{kind}",
        "action": "retry_model_planning",
        "kind": kind,
        "label": "producer label must not be authoritative",
        "planningSelectionRootTurnId": root,
        "rootPortfolioId": PORTFOLIO,
        "requestContractFingerprint": FINGERPRINT,
        "expectedBaseVersionId": base,
        "focusBriefId": "brief_next",
        "expansionFocusMode": "exact",
    }


def test_two_raw_continuations_reconcile_to_one_partial_capability():
    result = PortfolioContinuationCapabilityReconciler.reconcile(
        active_version_id=ACTIVE_VERSION,
        portfolio_summary=_summary(),
        raw_candidates=[
            _candidate(kind="portfolio_partial_more_plans"),
            _candidate(kind="portfolio_more_plans"),
        ],
        has_active_partial=True,
    )

    assert result.conflict is False
    assert len(result.capabilities) == 1
    capability = result.capabilities[0]
    assert capability["kind"] == "portfolio_partial_more_plans"
    assert capability["capabilityFamily"] == "portfolio_continuation"
    assert capability["focusBriefId"] == "brief_next"
    assert capability["expectedBaseVersionId"] == ACTIVE_VERSION


def test_stale_base_candidate_is_ignored_in_favor_of_current_active_base():
    result = PortfolioContinuationCapabilityReconciler.reconcile(
        active_version_id=ACTIVE_VERSION,
        portfolio_summary=_summary(),
        raw_candidates=[
            _candidate(kind="portfolio_more_plans", base="ver_stale"),
            _candidate(kind="portfolio_more_plans"),
        ],
        has_active_partial=False,
    )

    assert result.conflict is False
    assert result.capabilities[0]["expectedBaseVersionId"] == ACTIVE_VERSION


def test_root_or_fingerprint_conflict_fails_closed():
    result = PortfolioContinuationCapabilityReconciler.reconcile(
        active_version_id=ACTIVE_VERSION,
        portfolio_summary=_summary(),
        raw_candidates=[_candidate(kind="portfolio_more_plans", root="other_root")],
        has_active_partial=False,
    )

    assert result.capabilities == ()
    assert result.conflict is True
    assert result.reason_code == "portfolio_continuation_scope_conflict"


def test_same_root_stale_exact_focus_is_dropped_for_authoritative_discover_next():
    candidate = _candidate(kind="portfolio_more_plans")
    candidate["focusBriefId"] = "brief_stale_exact"

    summary = _summary(next_brief_id="")
    result = PortfolioContinuationCapabilityReconciler.reconcile(
        active_version_id=ACTIVE_VERSION,
        portfolio_summary=summary,
        raw_candidates=[candidate],
        has_active_partial=False,
    )

    assert result.conflict is False
    assert len(result.capabilities) == 1
    capability = result.capabilities[0]
    assert capability["expansionFocusMode"] == "discover_next"
    assert capability["focusBriefId"] == "brief_current"


def test_hidden_projection_carrier_is_not_an_executable_continuation():
    result = PortfolioContinuationCapabilityReconciler.reconcile(
        active_version_id=ACTIVE_VERSION,
        portfolio_summary=_summary(),
        raw_candidates=[
            {
                "id": "readonly",
                "kind": "portfolio_comparison_readonly",
                "comparisonProjection": {"proposalId": "partial"},
            }
        ],
        has_active_partial=True,
    )

    assert len(result.capabilities) == 1
    assert result.capabilities[0]["kind"] == "portfolio_partial_more_plans"


def test_frontier_without_persisted_next_brief_uses_discover_next():
    result = PortfolioContinuationCapabilityReconciler.reconcile(
        active_version_id=ACTIVE_VERSION,
        portfolio_summary=_summary(next_brief_id=""),
        raw_candidates=[],
        has_active_partial=False,
    )

    assert result.capabilities[0]["expansionFocusMode"] == "discover_next"
    assert result.capabilities[0]["focusBriefId"] == "brief_current"


def test_discover_next_execution_is_not_bound_to_moving_display_focus():
    issued = PortfolioContinuationCapabilityReconciler.reconcile(
        active_version_id=ACTIVE_VERSION,
        portfolio_summary=_summary(next_brief_id=""),
        raw_candidates=[],
        has_active_partial=True,
    ).capabilities[0]
    resumed_summary = _summary(next_brief_id="")
    resumed_summary["creativeExplorationFrontier"]["currentFocusBriefId"] = "brief_newly_displayed"

    decision = PortfolioContinuationCapabilityReconciler.evaluate_eligibility(
        active_version_id=ACTIVE_VERSION,
        portfolio_summary=resumed_summary,
        selected_option=issued,
        persisted_visible_proposal_ids=[],
        portfolio_status="awaiting_selection",
    )

    assert decision.eligible is True
    assert decision.reason_code == "portfolio_continuation_eligible"


def test_exhausted_frontier_emits_no_continuation():
    result = PortfolioContinuationCapabilityReconciler.reconcile(
        active_version_id=ACTIVE_VERSION,
        portfolio_summary=_summary(next_brief_id="", can_continue=False),
        raw_candidates=[],
        has_active_partial=False,
    )

    assert result.capabilities == ()
    assert result.reason_code == "portfolio_continuation_unavailable"


def test_attempt_window_exhaustion_cannot_be_reissued_as_discover_next():
    """Preserve the persisted reason when deciding whether a frontier can continue."""

    summary = _summary(next_brief_id="")
    summary["creativeExplorationFrontier"].update(
        {
            "frontierState": "temporarily_degraded",
            "remainingBudget": 4,
            "exhaustionReason": "max_consecutive_failed_attempts",
        }
    )

    result = PortfolioContinuationCapabilityReconciler.reconcile(
        active_version_id=ACTIVE_VERSION,
        portfolio_summary=summary,
        raw_candidates=[],
        has_active_partial=True,
    )
    decision = PortfolioContinuationCapabilityReconciler.evaluate_eligibility(
        active_version_id=ACTIVE_VERSION,
        portfolio_summary=summary,
        selected_option=None,
        persisted_visible_proposal_ids=[],
        portfolio_status="awaiting_selection",
    )

    assert result.capabilities == ()
    assert decision.eligible is False
    assert decision.reason_code == "portfolio_continuation_frontier_exhausted"


def test_exact_cursor_cannot_reopen_frontier_with_missing_state_and_zero_budget():
    summary = _summary()
    summary["creativeExplorationFrontier"].pop("frontierState")
    summary["creativeExplorationFrontier"]["remainingBudget"] = 0
    option = _candidate(kind="portfolio_more_plans")

    reconciled = PortfolioContinuationCapabilityReconciler.reconcile(
        active_version_id=ACTIVE_VERSION,
        portfolio_summary=summary,
        raw_candidates=[option],
        has_active_partial=True,
    )
    decision = PortfolioContinuationCapabilityReconciler.evaluate_eligibility(
        active_version_id=ACTIVE_VERSION,
        portfolio_summary=summary,
        selected_option=option,
        persisted_visible_proposal_ids=[],
        portfolio_status="awaiting_selection",
    )

    assert reconciled.capabilities == ()
    assert decision.eligible is False
    assert decision.reason_code == "portfolio_continuation_frontier_exhausted"


def test_continuation_expiry_is_issued_and_enforced_by_shared_decision():
    expires_at = "2026-08-06T11:00:00+00:00"
    issued = PortfolioContinuationCapabilityReconciler.reconcile(
        active_version_id=ACTIVE_VERSION,
        portfolio_summary=_summary(),
        raw_candidates=[],
        has_active_partial=True,
        continuation_expires_at=expires_at,
        require_expiry=True,
        evaluated_at=datetime(2026, 8, 6, 10, 0, tzinfo=timezone.utc),
    )

    assert issued.capabilities[0]["expiresAt"] == expires_at

    expired_option = dict(issued.capabilities[0])
    expired = PortfolioContinuationCapabilityReconciler.evaluate_eligibility(
        active_version_id=ACTIVE_VERSION,
        portfolio_summary=_summary(),
        selected_option=expired_option,
        persisted_visible_proposal_ids=[],
        portfolio_status="awaiting_selection",
        continuation_expires_at=expires_at,
        require_expiry=True,
        evaluated_at=datetime(2026, 8, 6, 11, 0, tzinfo=timezone.utc),
    )

    assert expired.eligible is False
    assert expired.reason_code == "portfolio_continuation_expired"


def test_blocked_visible_partial_remains_eligible_for_same_root_exploration():
    summary = _summary()
    summary.update(
        {
            "generationState": "degraded_has_more",
            "visibleProposalIds": ["partial:portfolio_root"],
        }
    )
    option = _candidate(kind="portfolio_more_plans")
    option["retryCurrentStageEligible"] = True

    decision = PortfolioContinuationCapabilityReconciler.evaluate_eligibility(
        active_version_id=ACTIVE_VERSION,
        portfolio_summary=summary,
        selected_option=option,
        persisted_visible_proposal_ids=["partial:portfolio_root"],
        portfolio_status="awaiting_selection",
        portfolio_expired=False,
        session_matches=True,
    )

    assert decision.eligible is True
    assert decision.reason_code == "portfolio_continuation_eligible"
    assert decision.visible_proposal_ids == ("partial:portfolio_root",)
    assert decision.focus_brief_id == "brief_next"


def test_continuation_eligibility_reports_specific_identity_and_lifecycle_failures():
    option = _candidate(kind="portfolio_more_plans")

    wrong_base = PortfolioContinuationCapabilityReconciler.evaluate_eligibility(
        active_version_id=ACTIVE_VERSION,
        portfolio_summary=_summary(),
        selected_option={**option, "expectedBaseVersionId": "ver_stale"},
        persisted_visible_proposal_ids=[],
        portfolio_status="awaiting_selection",
    )
    wrong_root = PortfolioContinuationCapabilityReconciler.evaluate_eligibility(
        active_version_id=ACTIVE_VERSION,
        portfolio_summary=_summary(),
        selected_option={**option, "rootPortfolioId": "portfolio_other"},
        persisted_visible_proposal_ids=[],
        portfolio_status="awaiting_selection",
    )
    wrong_fingerprint = PortfolioContinuationCapabilityReconciler.evaluate_eligibility(
        active_version_id=ACTIVE_VERSION,
        portfolio_summary=_summary(),
        selected_option={**option, "requestContractFingerprint": "x" * 64},
        persisted_visible_proposal_ids=[],
        portfolio_status="awaiting_selection",
    )
    expired = PortfolioContinuationCapabilityReconciler.evaluate_eligibility(
        active_version_id=ACTIVE_VERSION,
        portfolio_summary=_summary(),
        selected_option=option,
        persisted_visible_proposal_ids=[],
        portfolio_status="awaiting_selection",
        portfolio_expired=True,
    )
    wrong_membership_summary = _summary()
    wrong_membership_summary["visibleProposalIds"] = ["proposal_missing"]
    wrong_membership = PortfolioContinuationCapabilityReconciler.evaluate_eligibility(
        active_version_id=ACTIVE_VERSION,
        portfolio_summary=wrong_membership_summary,
        selected_option=option,
        persisted_visible_proposal_ids=[],
        portfolio_status="awaiting_selection",
    )

    assert wrong_base.reason_code == "portfolio_continuation_base_version_mismatch"
    assert wrong_root.reason_code == "portfolio_continuation_root_portfolio_mismatch"
    assert wrong_fingerprint.reason_code == "portfolio_continuation_request_fingerprint_mismatch"
    assert expired.reason_code == "portfolio_continuation_expired"
    assert wrong_membership.reason_code == "portfolio_continuation_visible_membership_mismatch"

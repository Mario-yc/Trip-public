"""Reconcile one authoritative Creative Portfolio continuation capability."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Iterable

from src.services.creative_exploration_frontier_service import (
    CreativeExplorationFrontierService,
)

_CONTINUATION_KINDS = {
    "portfolio_more_plans",
    "portfolio_partial_more_plans",
}


@dataclass(frozen=True)
class PortfolioContinuationReconciliation:
    capabilities: tuple[dict[str, Any], ...]
    conflict: bool
    reason_code: str
    trace: dict[str, Any]


@dataclass(frozen=True)
class ContinuationEligibilityDecision:
    eligible: bool
    reason_code: str
    planning_selection_root_turn_id: str | None
    root_portfolio_id: str | None
    request_contract_fingerprint: str | None
    expected_base_version_id: str | None
    focus_brief_id: str | None
    expansion_focus_mode: str | None
    expires_at: str | None
    visible_proposal_ids: tuple[str, ...]
    trace: dict[str, Any]


class PortfolioContinuationCapabilityReconciler:
    """Collapse producer-local continuation hints into one opaque capability.

    Persisted portfolio summary/frontier identity is authoritative. Producer
    labels and IDs are deliberately ignored; conflicting root identity fails
    closed so the capability resolver never sees two competing operations.
    """

    @classmethod
    def reconcile(
        cls,
        *,
        active_version_id: str | None,
        portfolio_summary: dict[str, Any] | None,
        raw_candidates: Iterable[dict[str, Any]] | None,
        has_active_partial: bool,
        persisted_visible_proposal_ids: Iterable[str] | None = None,
        portfolio_status: str | None = None,
        portfolio_expired: bool = False,
        session_matches: bool = True,
        continuation_expires_at: str | None = None,
        require_expiry: bool = False,
        evaluated_at: datetime | None = None,
    ) -> PortfolioContinuationReconciliation:
        summary = portfolio_summary if isinstance(portfolio_summary, dict) else {}
        eligibility = cls.evaluate_eligibility(
            active_version_id=active_version_id,
            portfolio_summary=summary,
            selected_option=None,
            persisted_visible_proposal_ids=persisted_visible_proposal_ids,
            portfolio_status=portfolio_status,
            portfolio_expired=portfolio_expired,
            session_matches=session_matches,
            continuation_expires_at=continuation_expires_at,
            require_expiry=require_expiry,
            evaluated_at=evaluated_at,
        )
        candidates = [
            item
            for item in raw_candidates or ()
            if isinstance(item, dict) and str(item.get("kind") or "") in _CONTINUATION_KINDS
        ]
        trace = {
            **eligibility.trace,
            "rawContinuationCount": len(candidates),
        }
        if not eligibility.eligible:
            return PortfolioContinuationReconciliation(
                capabilities=(),
                conflict=False,
                reason_code=(
                    "portfolio_continuation_unavailable"
                    if eligibility.reason_code
                    in {
                        "portfolio_continuation_frontier_exhausted",
                        "portfolio_continuation_focus_missing",
                        "portfolio_continuation_identity_missing",
                    }
                    else eligibility.reason_code
                ),
                trace={**trace, "emittedContinuationCount": 0},
            )

        root_id = str(eligibility.planning_selection_root_turn_id or "")
        portfolio_id = str(eligibility.root_portfolio_id or "")
        fingerprint = str(eligibility.request_contract_fingerprint or "")
        active_version = str(eligibility.expected_base_version_id or "")
        focus_brief_id = str(eligibility.focus_brief_id or "")
        focus_mode = str(eligibility.expansion_focus_mode or "")
        expected = {
            "planningSelectionRootTurnId": root_id,
            "rootPortfolioId": portfolio_id,
            "requestContractFingerprint": fingerprint,
            "focusBriefId": focus_brief_id,
            "expansionFocusMode": focus_mode,
        }
        identity_scope = {
            key: expected[key]
            for key in (
                "planningSelectionRootTurnId",
                "rootPortfolioId",
                "requestContractFingerprint",
            )
        }
        stale_producer_cursor_count = 0
        for candidate in candidates:
            for key, authoritative in identity_scope.items():
                observed = str(candidate.get(key) or "").strip()
                if observed and observed != authoritative:
                    return PortfolioContinuationReconciliation(
                        capabilities=(),
                        conflict=True,
                        reason_code="portfolio_continuation_scope_conflict",
                        trace={
                            **trace,
                            "emittedContinuationCount": 0,
                            "conflictField": key,
                            "conflictingValue": observed,
                        },
                    )
            producer_focus = str(candidate.get("focusBriefId") or "").strip()
            producer_mode = str(candidate.get("expansionFocusMode") or "").strip()
            if (producer_focus and producer_focus != focus_brief_id) or (producer_mode and producer_mode != focus_mode):
                stale_producer_cursor_count += 1

        kind = "portfolio_partial_more_plans" if has_active_partial else "portfolio_more_plans"
        label = "继续生成其他方案"
        payload = {
            "action": "retry_model_planning",
            "kind": kind,
            "label": label,
            "lifecycle": "offered",
            "capabilityFamily": "portfolio_continuation",
            "retryCurrentStageEligible": any(
                candidate.get("retryCurrentStageEligible") is True for candidate in candidates
            ),
            **expected,
            "expectedBaseVersionId": active_version or None,
        }
        if eligibility.expires_at:
            payload["expiresAt"] = eligibility.expires_at
        identity_payload = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        payload["id"] = "portfolio_continuation_" + hashlib.sha256(identity_payload.encode("utf-8")).hexdigest()[:24]
        return PortfolioContinuationReconciliation(
            capabilities=(payload,),
            conflict=False,
            reason_code="portfolio_continuation_reconciled",
            trace={
                **trace,
                "emittedContinuationCount": 1,
                "focusBriefId": focus_brief_id,
                "focusMode": focus_mode,
                "staleProducerCursorCount": stale_producer_cursor_count,
            },
        )

    @classmethod
    def evaluate_eligibility(
        cls,
        *,
        active_version_id: str | None,
        portfolio_summary: dict[str, Any] | None,
        selected_option: dict[str, Any] | None,
        persisted_visible_proposal_ids: Iterable[str] | None,
        portfolio_status: str | None = None,
        portfolio_expired: bool = False,
        session_matches: bool = True,
        continuation_expires_at: str | None = None,
        require_expiry: bool = False,
        evaluated_at: datetime | None = None,
    ) -> ContinuationEligibilityDecision:
        """Return the single pure authorization decision used at issue and execute time."""

        summary = portfolio_summary if isinstance(portfolio_summary, dict) else {}
        frontier = (
            summary.get("creativeExplorationFrontier")
            if isinstance(summary.get("creativeExplorationFrontier"), dict)
            else {}
        )
        root_id = str(summary.get("planningSelectionRootTurnId") or "").strip()
        portfolio_id = str(summary.get("rootPortfolioId") or summary.get("portfolioId") or "").strip()
        fingerprint = str(summary.get("requestContractFingerprint") or "").strip()
        active_version = str(
            active_version_id if active_version_id is not None else summary.get("expectedBaseVersionId") or ""
        ).strip()
        next_brief_id = str(summary.get("nextBriefId") or frontier.get("nextBriefId") or "").strip()
        current_focus = str(
            frontier.get("currentFocusBriefId") or summary.get("focusBriefId") or frontier.get("focusBriefId") or ""
        ).strip()
        frontier_state = str(frontier.get("frontierState") or summary.get("frontierState") or "").strip()
        remaining_budget = cls._integer(frontier.get("remainingBudget"), default=0)
        focus_mode = "exact" if next_brief_id else "discover_next"
        focus_brief_id = next_brief_id or current_focus
        expires_at = str(continuation_expires_at or "").strip()
        expiry_elapsed, expiry_invalid = cls._expiration_state(
            expires_at,
            evaluated_at=evaluated_at,
        )
        visible_ids = tuple(
            dict.fromkeys(str(item).strip() for item in summary.get("visibleProposalIds") or [] if str(item).strip())
        )
        persisted_ids = (
            set(visible_ids)
            if persisted_visible_proposal_ids is None
            else {str(item).strip() for item in persisted_visible_proposal_ids if str(item).strip()}
        )
        trace = {
            "planningSelectionRootTurnId": root_id or None,
            "rootPortfolioId": portfolio_id or None,
            "requestContractFingerprint": fingerprint or None,
            "expectedBaseVersionId": active_version or None,
            "frontierState": frontier_state or None,
            "remainingBudget": remaining_budget,
            "focusBriefId": focus_brief_id or None,
            "focusMode": focus_mode if focus_brief_id else None,
            "expiresAt": expires_at or None,
            "visibleProposalIds": list(visible_ids),
            "persistedVisibleProposalIds": sorted(persisted_ids),
        }

        def decision(eligible: bool, reason_code: str) -> ContinuationEligibilityDecision:
            return ContinuationEligibilityDecision(
                eligible=eligible,
                reason_code=reason_code,
                planning_selection_root_turn_id=root_id or None,
                root_portfolio_id=portfolio_id or None,
                request_contract_fingerprint=fingerprint or None,
                expected_base_version_id=active_version or None,
                focus_brief_id=focus_brief_id or None,
                expansion_focus_mode=focus_mode if focus_brief_id else None,
                expires_at=expires_at or None,
                visible_proposal_ids=visible_ids,
                trace={**trace, "eligibilityReasonCode": reason_code},
            )

        if not session_matches:
            return decision(False, "portfolio_continuation_session_mismatch")
        if require_expiry and not expires_at:
            return decision(False, "portfolio_continuation_expiry_missing")
        if expiry_invalid:
            return decision(False, "portfolio_continuation_expiry_invalid")
        if portfolio_expired or expiry_elapsed:
            return decision(False, "portfolio_continuation_expired")
        if portfolio_status and portfolio_status not in {"awaiting_selection", "failed"}:
            return decision(False, "portfolio_continuation_status_ineligible")
        if not all((root_id, portfolio_id, fingerprint)):
            return decision(False, "portfolio_continuation_identity_missing")
        if not focus_brief_id:
            return decision(False, "portfolio_continuation_focus_missing")
        # The complete persisted frontier is the authority for continuation.
        # Passing only state and budget would discard an attempt-window
        # exhaustion reason and could reissue a discover-next choice after the
        # bounded Provider/frontier loop has already stopped.
        frontier_continuable = CreativeExplorationFrontierService.can_continue(frontier)
        exact_cursor_continuable = bool(next_brief_id) and frontier_continuable
        dynamic_cursor_continuable = frontier_continuable
        if not (exact_cursor_continuable or dynamic_cursor_continuable):
            return decision(False, "portfolio_continuation_frontier_exhausted")
        if visible_ids and not set(visible_ids).issubset(persisted_ids):
            return decision(
                False,
                "portfolio_continuation_visible_membership_mismatch",
            )

        option = selected_option if isinstance(selected_option, dict) else None
        if option is not None:
            if str(option.get("kind") or "") not in _CONTINUATION_KINDS:
                return decision(False, "portfolio_continuation_kind_mismatch")
            for key, authoritative, reason_code in (
                (
                    "planningSelectionRootTurnId",
                    root_id,
                    "portfolio_continuation_planning_root_mismatch",
                ),
                (
                    "rootPortfolioId",
                    portfolio_id,
                    "portfolio_continuation_root_portfolio_mismatch",
                ),
                (
                    "requestContractFingerprint",
                    fingerprint,
                    "portfolio_continuation_request_fingerprint_mismatch",
                ),
            ):
                if str(option.get(key) or "").strip() != authoritative:
                    return decision(False, reason_code)
            if str(option.get("expectedBaseVersionId") or "").strip() != active_version:
                return decision(False, "portfolio_continuation_base_version_mismatch")
            observed_expires_at = str(option.get("expiresAt") or "").strip()
            if observed_expires_at and expires_at and observed_expires_at != expires_at:
                return decision(False, "portfolio_continuation_expiry_mismatch")
            observed_mode = str(option.get("expansionFocusMode") or "").strip()
            if observed_mode and observed_mode != focus_mode:
                return decision(False, "portfolio_continuation_focus_mode_mismatch")
            # ``discover_next`` has no authoritative next brief yet.  Its
            # current focus is display context only and may legitimately move
            # when a newly appended proposal becomes the latest explored
            # direction.  Exact cursors remain bound to one brief.
            if focus_mode == "exact" and str(option.get("focusBriefId") or "").strip() != focus_brief_id:
                return decision(False, "portfolio_continuation_focus_mismatch")

        return decision(True, "portfolio_continuation_eligible")

    @staticmethod
    def _expiration_state(
        value: str,
        *,
        evaluated_at: datetime | None,
    ) -> tuple[bool, bool]:
        if not value:
            return False, False
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False, True
        if parsed.tzinfo is None:
            return False, True
        current = evaluated_at or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return (
            parsed.astimezone(timezone.utc) <= current.astimezone(timezone.utc),
            False,
        )

    @staticmethod
    def _integer(value: Any, *, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

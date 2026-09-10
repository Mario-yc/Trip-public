"""The sole proposal-to-itinerary hand-off.  It delegates all writes to a callback."""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Callable, Optional

from src.services.agent_run_control import begin_session_run_write
from src.services.plan_portfolio_store import PlanPortfolioStore
from src.core.config import get_settings


class PlanProposalCommitService:
    def __init__(self, store: PlanPortfolioStore) -> None:
        self.store = store

    def commit(
        self,
        *,
        session_id: str,
        source_user_turn_id: str,
        choice_id: str,
        active_version_id: Optional[str],
        verify: Callable[[dict[str, Any]], bool],
        persist: Callable[[dict[str, Any]], str],
    ) -> str:
        loaded = self.store.load_choice(session_id=session_id, source_user_turn_id=source_user_turn_id, choice_id=choice_id)
        if loaded is None:
            raise ValueError("plan_proposal_choice_not_found")
        portfolio, proposal = loaded
        if portfolio["status"] == "committed" and proposal["status"] == "committed":
            raise ValueError("plan_proposal_committed_result_must_be_replayed_by_choice_execution")
        if portfolio["status"] == "expired" or portfolio.get("failure_reason") == "plan_portfolio_expired":
            raise ValueError("plan_proposal_expired")
        if portfolio["status"] != "awaiting_selection":
            raise ValueError("plan_portfolio_not_awaiting_selection")
        if str(proposal["id"]) not in set(portfolio.get("visible_proposal_ids") or []):
            raise ValueError("plan_proposal_not_visible")
        try:
            verifier = json.loads(str(proposal.get("verifier_json") or "{}"))
        except (TypeError, ValueError) as error:
            raise ValueError("plan_proposal_verifier_evidence_invalid") from error
        simple_direction_partial_allowed = bool(
            str(portfolio.get("workflow_mode") or "") == "simple_direction_v1"
            and verifier.get("simpleOpenDirectionVerified") is True
            and not [str(item) for item in verifier.get("hardFailures") or [] if str(item)]
        )
        if str(portfolio.get("workflow_mode") or "") == "simple_direction_v1":
            try:
                generation_lineage = json.loads(
                    str(proposal.get("generation_lineage_json") or "{}")
                )
            except (TypeError, ValueError) as error:
                raise ValueError("plan_proposal_request_fingerprint_invalid") from error
            frozen_request_fingerprint = str(
                portfolio.get("request_contract_fingerprint") or ""
            )
            if (
                not isinstance(generation_lineage, dict)
                or not frozen_request_fingerprint
                or str(generation_lineage.get("requestContractFingerprint") or "")
                != frozen_request_fingerprint
            ):
                raise ValueError("plan_proposal_request_fingerprint_invalid")
            try:
                frozen_snapshot = json.loads(str(proposal.get("snapshot_json") or "{}"))
            except (TypeError, ValueError) as error:
                raise ValueError("plan_proposal_snapshot_fingerprint_invalid") from error
            if (
                not isinstance(frozen_snapshot, dict)
                or str(generation_lineage.get("proposalSnapshotFingerprint") or "")
                != self._fingerprint(frozen_snapshot)
            ):
                raise ValueError("plan_proposal_snapshot_fingerprint_invalid")
            compact_route_contract = isinstance(
                frozen_snapshot.get("routeDecisionContract"), dict
            ) and isinstance(
                frozen_snapshot["routeDecisionContract"].get("adjacentLegConstraint"),
                dict,
            )
            if compact_route_contract and verifier.get("confirmationPassed") is not True:
                raise ValueError("plan_proposal_compact_route_not_confirmable")
        editable_draft_allowed = bool(
            (get_settings().agent_soft_slot_draft_adoption_enabled or simple_direction_partial_allowed)
            and verifier.get("draftPassed") is True
            and int(
                verifier.get("blockingPendingHardSlotCount")
                if verifier.get("blockingPendingHardSlotCount") is not None
                else verifier.get("pendingHardSlotCount") or 0
            )
            == 0
        )
        route_reverifiable = bool(
            str(portfolio.get("workflow_mode") or "") != "simple_direction_v1"
            and self._route_reverifiable_failure(snapshot=proposal, verifier=verifier)
        )
        if verifier.get("passed") is not True and not editable_draft_allowed and not route_reverifiable:
            raise ValueError("plan_proposal_verifier_not_passed")
        expected = str(portfolio.get("expected_base_version_id") or "") or None
        if expected != active_version_id:
            raise ValueError("plan_proposal_base_version_stale")
        snapshot = self._adopted_snapshot(
            json.loads(str(proposal["snapshot_json"])),
            portfolio=portfolio,
            proposal=proposal,
            editable_draft_allowed=editable_draft_allowed,
            verifier=verifier,
        )
        verified = verify(snapshot)
        if not verified:
            raise ValueError("plan_proposal_verifier_failed")
        begin_session_run_write(session_id)
        if not self.store.claim_selection(portfolio_id=str(portfolio["id"]), proposal_id=str(proposal["id"])):
            raise ValueError("plan_proposal_selection_in_progress_or_stale")
        try:
            result_version_id = persist(snapshot)
        except Exception:
            self.store.release_selection(
                portfolio_id=str(portfolio["id"]),
                reason="plan_proposal_persist_failed",
                restore_proposal_id=(
                    str(portfolio.get("selected_proposal_id") or "") or None
                    if str(portfolio.get("workflow_mode") or "") == "simple_direction_v1"
                    else None
                ),
            )
            raise
        try:
            self.store.mark_committed(
                portfolio_id=str(portfolio["id"]),
                proposal_id=str(proposal["id"]),
                result_version_id=result_version_id,
            )
        except Exception:
            # The authoritative version is already committed.  Reconcile this
            # narrow post-writer bookkeeping window from server-owned DB
            # identities so the same opaque choice cannot create version N+1.
            self.store.reconcile_committed_selection(
                portfolio_id=str(portfolio["id"]),
                proposal_id=str(proposal["id"]),
                result_version_id=result_version_id,
            )
        return result_version_id

    @staticmethod
    def _adopted_snapshot(
        snapshot: dict[str, Any],
        *,
        portfolio: Optional[dict[str, Any]] = None,
        proposal: Optional[dict[str, Any]] = None,
        editable_draft_allowed: bool = False,
        verifier: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Remove proposal-preview wording before the snapshot becomes active."""

        adopted = copy.deepcopy(snapshot)
        rationale = str(adopted.get("decisionRationale") or "")
        if "本方案尚未写入正式行程" in rationale:
            adopted["decisionRationale"] = rationale.replace(
                "本方案尚未写入正式行程。",
                "已采用为当前可编辑行程，后续修改继续通过版本事务保存。",
            ).replace(
                "本方案尚未写入正式行程",
                "已采用为当前可编辑行程，后续修改继续通过版本事务保存",
            )
        pending_slots = [
            item
            for item in adopted.get("portfolioPendingSlots") or []
            if isinstance(item, dict) and str(item.get("state") or "pending") != "completed"
        ]
        if editable_draft_allowed and pending_slots and portfolio and proposal:
            try:
                brief = json.loads(str(proposal.get("brief_json") or "{}"))
            except (TypeError, ValueError):
                brief = {}
            focus_brief_id = str(
                brief.get("briefId")
                or adopted.get("portfolioFocusBriefId")
                or ""
            ).strip()
            adopted["status"] = "partial"
            adopted["portfolioPartialTimeline"] = {
                "status": "partial",
                "sourceProposalId": str(proposal.get("id") or "") or None,
                "strictProposalVerifierPassed": bool((verifier or {}).get("passed")),
                "pendingSlotCount": len(pending_slots),
                "pendingSlotsStatus": "needs_confirmation",
            }
            adopted["portfolioSelectionContext"] = {
                "planningSelectionRootTurnId": str(portfolio.get("source_user_turn_id") or ""),
                "rootPortfolioId": str(portfolio.get("id") or ""),
                "focusBriefId": focus_brief_id,
                "sourceUserTurnId": str(portfolio.get("source_user_turn_id") or ""),
                "requestContractFingerprint": str(
                    portfolio.get("request_contract_fingerprint") or ""
                ),
            }
        return adopted

    @staticmethod
    def _route_reverifiable_failure(*, snapshot: dict[str, Any], verifier: dict[str, Any]) -> bool:
        """Allow one strict route recheck for any route-only proposal failure.

        A full or partial proposal may retain a failed/partial route preflight
        after the Provider recovers.  It is still never writable until the
        caller's authoritative recheck succeeds and the normal versioned writer
        verifies the result.  Non-route hard failures remain terminal here.
        """
        try:
            material = json.loads(str(snapshot.get("snapshot_json") or "{}"))
        except (TypeError, ValueError):
            return False
        if not isinstance(material, dict):
            return False
        if not material.get("days") and not (
            isinstance(material.get("portfolioPartialTimeline"), dict)
            or str(material.get("originProjectionMode") or "") == "partial_preview"
            or str(material.get("comparisonRole") or "") == "current_active_draft"
        ):
            return False
        failures = [
            str(item)
            for key in ("hardFailures", "routeCoverageFailures", "routeQualityFailures")
            for item in verifier.get(key) or []
            if str(item)
        ]
        return bool(failures) and all(
            ("route" in item.casefold() or "preflight" in item.casefold())
            for item in failures
        )

    @staticmethod
    def _fingerprint(value: dict[str, Any]) -> str:
        return hashlib.sha256(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()

"""Durable opaque checkpoints for Creative Portfolio continuation choices.

The checkpoint is persisted inside the assistant turn's choice option.  It is
therefore covered by the existing opaque-choice resolver and execution CAS,
without adding a second writer or a database migration.
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Iterable


class PortfolioContinuationCheckpointService:
    SCHEMA_VERSION = "creative-portfolio-continuation-checkpoint-v1"
    DELEGATED_SCHEMA_VERSIONS = {"portfolio-density-continuation-v1"}
    EXECUTABLE_KINDS = {
        "portfolio_more_plans",
        "portfolio_partial_more_plans",
        "portfolio_density_candidate",
        "portfolio_density_refresh",
        "portfolio_density_nearby",
        "portfolio_density_manual",
        "portfolio_pending_slot_candidate",
        "portfolio_pending_slot_refresh",
        "portfolio_pending_slot_nearby",
        "portfolio_pending_slot_manual",
        "custom_input",
        "plan_proposal",
    }

    @classmethod
    def attach(
        cls,
        options: Iterable[dict[str, Any]],
        *,
        session_id: str,
        source_assistant_turn_id: str,
        portfolio_id: str,
        portfolio_summary: dict[str, Any] | None,
        default_planning_root_id: str,
        default_request_fingerprint: str,
        default_expected_base_version_id: str | None,
        default_proposal_id: str = "",
        expires_at: str | None = None,
    ) -> list[dict[str, Any]]:
        summary = portfolio_summary if isinstance(portfolio_summary, dict) else {}
        frontier = (
            summary.get("creativeExplorationFrontier")
            if isinstance(summary.get("creativeExplorationFrontier"), dict)
            else {}
        )
        visible_ids = [
            str(item) for item in summary.get("visibleProposalIds") or summary.get("proposalIds") or [] if str(item)
        ]
        output: list[dict[str, Any]] = []
        for raw in options:
            option = copy.deepcopy(raw)
            kind = str(option.get("kind") or "")
            if (
                kind not in cls.EXECUTABLE_KINDS
                or str(option.get("lifecycle") or "offered") == "hidden"
                or not str(option.get("action") or "")
            ):
                output.append(option)
                continue
            root_id = str(
                option.get("planningSelectionRootTurnId")
                or default_planning_root_id
                or summary.get("planningSelectionRootTurnId")
                or ""
            )
            root_portfolio_id = str(
                option.get("rootPortfolioId") or portfolio_id or summary.get("rootPortfolioId") or ""
            )
            request_fingerprint = str(
                option.get("requestContractFingerprint")
                or default_request_fingerprint
                or summary.get("requestContractFingerprint")
                or ""
            )
            projection = (
                option.get("comparisonProjection") if isinstance(option.get("comparisonProjection"), dict) else {}
            )
            proposal_id = str(
                option.get("proposalId")
                or projection.get("proposalId")
                or default_proposal_id
                or (f"partial:{root_portfolio_id}" if root_portfolio_id else "")
            )
            checkpoint = {
                "schemaVersion": cls.SCHEMA_VERSION,
                "checkpointState": "offered",
                "sessionId": str(session_id),
                "planningRootId": root_id,
                "planningSelectionRootTurnId": root_id,
                "portfolioId": root_portfolio_id,
                "rootPortfolioId": root_portfolio_id,
                "proposalId": proposal_id,
                "briefId": str(option.get("briefId") or option.get("focusBriefId") or ""),
                "focusBriefId": str(option.get("focusBriefId") or option.get("briefId") or ""),
                "poolId": str(option.get("poolId") or ""),
                "planningSlotId": str(option.get("planningSlotId") or option.get("slotId") or ""),
                "dayNumber": int(option.get("dayNumber") or 0),
                "sourceAssistantTurnId": str(source_assistant_turn_id),
                "choiceId": str(option.get("id") or ""),
                "requestFingerprint": request_fingerprint,
                "requestContractFingerprint": request_fingerprint,
                "expectedBaseVersionId": option.get("expectedBaseVersionId", default_expected_base_version_id),
                "frontierCursor": {
                    "frontierState": str(frontier.get("frontierState") or summary.get("generationState") or ""),
                    "nextBriefId": str(summary.get("nextBriefId") or frontier.get("nextBriefId") or ""),
                    "currentFocusBriefId": str(
                        frontier.get("currentFocusBriefId") or summary.get("focusBriefId") or ""
                    ),
                    "continuationRound": int(frontier.get("continuationRound") or 0),
                    "remainingBudget": int(frontier.get("remainingBudget") or 0),
                },
                "attemptedDirectionSignatures": cls._strings(frontier.get("attemptedDirectionSignatures")),
                "acceptedDirectionSignatures": cls._strings(frontier.get("acceptedDirectionSignatures")),
                "rejectedDirectionSignatures": cls._strings(frontier.get("rejectedDirectionSignatures")),
                "inFlightDirectionSignatures": cls._strings(frontier.get("inFlightDirectionSignatures")),
                "visibleProposalIds": visible_ids,
                "expiresAt": str(option.get("expiresAt") or expires_at or ""),
            }
            checkpoint["checkpointFingerprint"] = cls.fingerprint(checkpoint)
            option["sourceAssistantTurnId"] = str(source_assistant_turn_id)
            option.setdefault("planningSelectionRootTurnId", root_id)
            option.setdefault("rootPortfolioId", root_portfolio_id)
            option.setdefault("requestContractFingerprint", request_fingerprint)
            # Continuation choices keep their proposal scope inside the signed
            # checkpoint.  Adding ``proposalId`` to an outer continuation
            # capability would make legacy clients misclassify it as adoption.
            if option.get("proposalId"):
                option["proposalId"] = str(option["proposalId"])
            if checkpoint["briefId"]:
                option.setdefault("briefId", checkpoint["briefId"])
            if expires_at:
                option.setdefault("expiresAt", str(expires_at))
            option["continuationCheckpoint"] = checkpoint
            option["checkpointFingerprint"] = checkpoint["checkpointFingerprint"]
            output.append(option)
        return output

    @classmethod
    def validate(
        cls,
        checkpoint: dict[str, Any],
        *,
        session_id: str,
        source_assistant_turn_id: str,
        choice_id: str,
        option: dict[str, Any],
        active_version_id: str | None,
        now: datetime | None = None,
    ) -> str | None:
        if not checkpoint:
            return None
        # Pre-v1 choices carry a smaller continuation payload that is still
        # validated by the existing root/version/slot guards.  Only an explicit
        # v1 schema opts into this additional signed-checkpoint contract.
        if "schemaVersion" not in checkpoint:
            return None
        schema_version = str(checkpoint.get("schemaVersion") or "")
        # Density continuations have their own durable validator and reach this
        # common execution claim only after that service has accepted them.
        if schema_version in cls.DELEGATED_SCHEMA_VERSIONS:
            return None
        if schema_version != cls.SCHEMA_VERSION:
            return "portfolio_checkpoint_schema_mismatch"
        expected_fingerprint = str(checkpoint.get("checkpointFingerprint") or "")
        if not expected_fingerprint or expected_fingerprint != cls.fingerprint(checkpoint):
            return "portfolio_checkpoint_fingerprint_mismatch"
        identity_checks = (
            ("sessionId", session_id, "portfolio_checkpoint_session_mismatch"),
            ("sourceAssistantTurnId", source_assistant_turn_id, "portfolio_checkpoint_source_turn_mismatch"),
            ("choiceId", choice_id, "portfolio_checkpoint_choice_mismatch"),
            (
                "planningSelectionRootTurnId",
                option.get("planningSelectionRootTurnId"),
                "portfolio_checkpoint_planning_root_mismatch",
            ),
            ("rootPortfolioId", option.get("rootPortfolioId"), "portfolio_checkpoint_portfolio_mismatch"),
            (
                "requestContractFingerprint",
                option.get("requestContractFingerprint"),
                "portfolio_checkpoint_request_fingerprint_mismatch",
            ),
        )
        for key, expected, reason in identity_checks:
            if str(checkpoint.get(key) or "") != str(expected or ""):
                return reason
        scoped_checks = (
            (
                "proposalId",
                option.get("proposalId") or (option.get("comparisonProjection") or {}).get("proposalId"),
                "portfolio_checkpoint_proposal_mismatch",
            ),
            ("briefId", option.get("briefId") or option.get("focusBriefId"), "portfolio_checkpoint_brief_mismatch"),
            ("poolId", option.get("poolId"), "portfolio_checkpoint_pool_mismatch"),
            (
                "planningSlotId",
                option.get("planningSlotId") or option.get("slotId"),
                "portfolio_checkpoint_slot_mismatch",
            ),
        )
        for key, expected, reason in scoped_checks:
            if expected not in (None, "") and str(checkpoint.get(key) or "") != str(expected):
                return reason
        checkpoint_base = str(checkpoint.get("expectedBaseVersionId") or "")
        option_base = str(option.get("expectedBaseVersionId") or "")
        if checkpoint_base != option_base or checkpoint_base != str(active_version_id or ""):
            return "portfolio_checkpoint_base_version_mismatch"
        expires_at = str(checkpoint.get("expiresAt") or "")
        if expires_at:
            try:
                expires = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                if expires.tzinfo is None:
                    return "portfolio_checkpoint_expiry_invalid"
            except ValueError:
                return "portfolio_checkpoint_expiry_invalid"
            if expires <= (now or datetime.now(timezone.utc)):
                return "portfolio_checkpoint_expired"
        return None

    @staticmethod
    def fingerprint(payload: dict[str, Any]) -> str:
        canonical = copy.deepcopy(payload)
        canonical.pop("checkpointFingerprint", None)
        encoded = json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _strings(value: Any) -> list[str]:
        return list(dict.fromkeys(str(item) for item in value or [] if str(item)))

"""SQLite source of truth for immutable proposal evidence and selection CAS."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import copy
import hashlib
import json
import sqlite3
from typing import Any, Optional

from src.core.config import get_settings
from src.services.creative_planning_models import (
    PlanCandidate,
    PlanPortfolio,
    proposal_canonical_signature,
)
from src.services.creative_exploration_frontier_service import (
    CreativeExplorationFrontierService,
)
from src.services.creative_proposal_title_service import CreativeProposalTitleService
from src.services.proposal_readiness_service import ProposalReadinessService
from src.services.simple_direction_frontier_service import SimpleDirectionFrontierService
from src.services.entity_qualification_evidence_service import EntityQualificationEvidenceService


class PlanPortfolioStore:
    SELECTION_LEASE_SECONDS = 120
    PORTFOLIO_TTL_MINUTES = 60

    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db
        settings = get_settings()
        self.max_generated_proposals_per_root = int(settings.agent_creative_portfolio_max_generated_proposals)
        self.max_visible_comparison_cards = int(settings.agent_creative_portfolio_max_visible_proposals)
        self.max_initial_proposals = min(
            self.max_generated_proposals_per_root,
            max(
                int(settings.agent_creative_portfolio_target_count),
                int(settings.agent_creative_portfolio_initial_batch_size),
            ),
        )

    def create(
        self,
        portfolio: PlanPortfolio,
        proposals: list[PlanCandidate],
        *,
        expires_at: Optional[str] = None,
        simple_direction_frontier: Optional[dict[str, Any]] = None,
    ) -> None:
        if portfolio.status not in {"building", "awaiting_selection", "failed"}:
            raise ValueError("new_portfolio_must_be_building_awaiting_or_failed")
        if portfolio.status == "failed" and proposals:
            raise ValueError("failed_portfolio_must_not_persist_proposals")
        if len(proposals) > self.max_initial_proposals:
            raise ValueError("portfolio_proposal_limit_exceeded")
        if any(item.portfolio_id != portfolio.portfolio_id for item in proposals):
            raise ValueError("proposal_portfolio_identity_mismatch")
        canonical_signatures = [str(item.canonical_signature or "").strip() for item in proposals]
        if any(not item for item in canonical_signatures):
            raise ValueError("canonical_signature_missing")
        if len(canonical_signatures) != len(set(canonical_signatures)):
            raise ValueError("duplicate_canonical_signature")
        existing = self.db.execute(
            """SELECT id, source_assistant_turn_id, expected_base_version_id,
                      source_observation_fingerprint, request_contract_fingerprint,
                      status, selected_proposal_id, dominant_proposal_id, failure_reason
               FROM agent_plan_portfolios
               WHERE session_id = ? AND source_user_turn_id = ?""",
            (portfolio.session_id, portfolio.source_user_turn_id),
        ).fetchone()
        if existing is not None:
            existing_proposals = self.db.execute(
                """SELECT id, canonical_signature
                   FROM agent_plan_proposals
                   WHERE portfolio_id = ?
                   ORDER BY rank_index ASC""",
                (existing["id"],),
            ).fetchall()
            existing_identity = (
                existing["id"],
                existing["source_assistant_turn_id"],
                existing["expected_base_version_id"],
                existing["source_observation_fingerprint"],
                existing["request_contract_fingerprint"],
                existing["status"],
                existing["selected_proposal_id"],
                existing["dominant_proposal_id"],
                existing["failure_reason"],
                [(row["id"], row["canonical_signature"]) for row in existing_proposals],
            )
            requested_identity = (
                portfolio.portfolio_id,
                portfolio.source_assistant_turn_id,
                portfolio.expected_base_version_id,
                portfolio.source_observation_fingerprint,
                portfolio.request_contract_fingerprint,
                portfolio.status,
                portfolio.selected_proposal_id,
                portfolio.dominant_proposal_id,
                portfolio.failure_reason,
                [(item.proposal_id, item.canonical_signature) for item in proposals],
            )
            if existing_identity == requested_identity:
                return
            raise ValueError("portfolio_root_identity_conflict")
        now = self._now()
        expires_at = (
            expires_at or (datetime.now(timezone.utc) + timedelta(minutes=self.PORTFOLIO_TTL_MINUTES)).isoformat()
        )
        summary = self._sync_summary_identity(
            portfolio.model_dump(by_alias=True),
            portfolio_id=portfolio.portfolio_id,
            planning_root_id=portfolio.source_user_turn_id,
            request_fingerprint=portfolio.request_contract_fingerprint,
            status=portfolio.status,
        )
        summary["visibleProposalIds"] = [str(item) for item in summary.get("visibleProposalIds") or [] if str(item)][
            : self.max_visible_comparison_cards
        ]
        if simple_direction_frontier is not None:
            SimpleDirectionFrontierService._validate(simple_direction_frontier)
            if (
                simple_direction_frontier.get("planningRootId") != portfolio.source_user_turn_id
                or simple_direction_frontier.get("requestContractFingerprint") != portfolio.request_contract_fingerprint
            ):
                raise ValueError("simple_direction_frontier_identity_mismatch")
            # Freeze selection and root in the same INSERT, including a crash
            # before the caller marks the workflow or freezes its contract.
            summary["simpleDirectionFrontier"] = copy.deepcopy(simple_direction_frontier)
            summary["simpleDirectionFrontierAttempts"] = {}
        self.db.execute(
            """INSERT INTO agent_plan_portfolios (
                id, session_id, source_user_turn_id, source_assistant_turn_id, expected_base_version_id,
                source_observation_fingerprint, request_contract_fingerprint, status,
                selected_proposal_id, dominant_proposal_id, summary_json, failure_reason,
                expires_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                portfolio.portfolio_id,
                portfolio.session_id,
                portfolio.source_user_turn_id,
                portfolio.source_assistant_turn_id,
                portfolio.expected_base_version_id,
                portfolio.source_observation_fingerprint,
                portfolio.request_contract_fingerprint,
                portfolio.status,
                portfolio.selected_proposal_id,
                portfolio.dominant_proposal_id,
                self._dump(summary),
                portfolio.failure_reason,
                expires_at,
                now,
                now,
            ),
        )
        for index, proposal in enumerate(proposals):
            truthful_snapshot = self._truthful_snapshot_title(
                proposal.itinerary_snapshot,
                proposal.verifier,
            )
            self.db.execute(
                """INSERT INTO agent_plan_proposals (
                    id, portfolio_id, choice_id, rank_index, status, brief_json, snapshot_json,
                    score_json, verifier_json, evidence_json, canonical_signature,
                    generation_lineage_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'offered', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    proposal.proposal_id,
                    portfolio.portfolio_id,
                    self._choice_id(proposal),
                    index,
                    self._dump(proposal.brief.model_dump(by_alias=True)),
                    self._dump(truthful_snapshot),
                    self._dump(proposal.score.model_dump(by_alias=True)),
                    self._dump(proposal.verifier),
                    self._dump({"grounded": proposal.grounded_evidence, "unresolved": proposal.unresolved_evidence}),
                    proposal.canonical_signature,
                    self._dump(proposal.generation_lineage),
                    now,
                    now,
                ),
            )
        self._persist_classification_counts(portfolio.portfolio_id)
        self.db.commit()

    def summary(self, *, portfolio_id: str) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT summary_json, expected_base_version_id FROM agent_plan_portfolios WHERE id = ?",
            (portfolio_id,),
        ).fetchone()
        if row is None:
            return {}
        try:
            summary = json.loads(str(row["summary_json"] or "{}"))
        except (TypeError, ValueError):
            summary = {}
        if isinstance(summary, dict):
            summary.setdefault("expectedBaseVersionId", str(row["expected_base_version_id"] or ""))
            return summary
        return {}

    def initialize_simple_direction_frontier(
        self,
        *,
        portfolio_id: str,
        frontier: dict[str, Any],
        expected_request_contract_fingerprint: str,
    ) -> dict[str, Any]:
        """Freeze one server-authored Simple Direction entity frontier.

        The Portfolio ``summary_json`` is the only cursor authority.  An
        existing frontier is returned only when its immutable root/request and
        qualification evidence identities still match; callers cannot replace
        it with a newly ordered client/model supplied list.
        """

        for _attempt in range(4):
            row = self.db.execute(
                """SELECT summary_json, request_contract_fingerprint, status,
                    source_user_turn_id
                FROM agent_plan_portfolios WHERE id = ?""",
                (portfolio_id,),
            ).fetchone()
            if row is None:
                raise ValueError("plan_portfolio_not_found")
            request_fingerprint = str(row["request_contract_fingerprint"] or "")
            if request_fingerprint != str(expected_request_contract_fingerprint or ""):
                raise ValueError("portfolio_request_fingerprint_mismatch")
            raw_summary = str(row["summary_json"] or "{}")
            try:
                summary = json.loads(raw_summary)
            except (TypeError, ValueError):
                summary = {}
            existing = summary.get("simpleDirectionFrontier")
            if isinstance(existing, dict):
                if (
                    str(existing.get("schemaVersion") or "") != SimpleDirectionFrontierService.SCHEMA_VERSION
                    or str(existing.get("planningRootId") or "") != str(row["source_user_turn_id"] or "")
                    or str(existing.get("requestContractFingerprint") or "") != request_fingerprint
                    or str(existing.get("qualificationEvidenceFingerprint") or "")
                    != str(frontier.get("qualificationEvidenceFingerprint") or "")
                ):
                    raise ValueError("simple_direction_frontier_identity_mismatch")
                return copy.deepcopy(existing)
            normalized = copy.deepcopy(frontier)
            if (
                str(normalized.get("schemaVersion") or "") != SimpleDirectionFrontierService.SCHEMA_VERSION
                or str(normalized.get("planningRootId") or "") != str(row["source_user_turn_id"] or "")
                or str(normalized.get("requestContractFingerprint") or "") != request_fingerprint
            ):
                raise ValueError("simple_direction_frontier_identity_mismatch")
            summary["simpleDirectionFrontier"] = normalized
            summary.setdefault("simpleDirectionFrontierAttempts", {})
            summary = self._sync_summary_identity(
                summary,
                portfolio_id=portfolio_id,
                planning_root_id=str(row["source_user_turn_id"] or ""),
                request_fingerprint=request_fingerprint,
                status=str(row["status"] or ""),
            )
            updated = self.db.execute(
                """UPDATE agent_plan_portfolios SET summary_json = ?, updated_at = ?
                WHERE id = ? AND summary_json = ?""",
                (self._dump(summary), self._now(), portfolio_id, raw_summary),
            )
            if updated.rowcount == 1:
                self.db.commit()
                return copy.deepcopy(normalized)
            self.db.rollback()
        raise ValueError("simple_direction_frontier_stale")

    def initialize_simple_direction_compatibility_frontier(
        self,
        *,
        portfolio_id: str,
        expected_request_contract_fingerprint: str,
        max_pages_per_query: int,
        page_offset: int = 5,
    ) -> dict[str, Any]:
        """Freeze the bounded generic POI page frontier on the planning root."""

        normalized_max_pages = max(1, int(max_pages_per_query or 1))
        normalized_page_offset = max(1, int(page_offset or 1))
        for _attempt in range(4):
            row = self.db.execute(
                """SELECT summary_json, request_contract_fingerprint, status,
                          source_user_turn_id
                   FROM agent_plan_portfolios WHERE id = ?""",
                (portfolio_id,),
            ).fetchone()
            if row is None:
                raise ValueError("plan_portfolio_not_found")
            request_fingerprint = str(row["request_contract_fingerprint"] or "")
            if request_fingerprint != str(expected_request_contract_fingerprint or ""):
                raise ValueError("portfolio_request_fingerprint_mismatch")
            raw_summary = str(row["summary_json"] or "{}")
            try:
                summary = json.loads(raw_summary)
            except (TypeError, ValueError):
                summary = {}
            existing = summary.get("simpleDirectionCompatibilityFrontier")
            if isinstance(existing, dict):
                if (
                    str(existing.get("schemaVersion") or "") != "simple-direction-compatibility-frontier-v1"
                    or str(existing.get("planningRootId") or "") != str(row["source_user_turn_id"] or "")
                    or str(existing.get("requestContractFingerprint") or "") != request_fingerprint
                ):
                    raise ValueError("simple_direction_compatibility_frontier_identity_mismatch")
                return copy.deepcopy(existing)
            frozen = {
                "schemaVersion": "simple-direction-compatibility-frontier-v1",
                "planningRootId": str(row["source_user_turn_id"] or ""),
                "requestContractFingerprint": request_fingerprint,
                "initialProviderPage": 1,
                "pageOffset": normalized_page_offset,
                "maxPagesPerQuery": normalized_max_pages,
            }
            summary["simpleDirectionCompatibilityFrontier"] = frozen
            summary.setdefault("simpleDirectionCompatibilityAttempts", {})
            summary = self._sync_summary_identity(
                summary,
                portfolio_id=portfolio_id,
                planning_root_id=str(row["source_user_turn_id"] or ""),
                request_fingerprint=request_fingerprint,
                status=str(row["status"] or ""),
            )
            updated = self.db.execute(
                """UPDATE agent_plan_portfolios SET summary_json = ?, updated_at = ?
                WHERE id = ? AND summary_json = ?""",
                (self._dump(summary), self._now(), portfolio_id, raw_summary),
            )
            if updated.rowcount == 1:
                self.db.commit()
                return copy.deepcopy(frozen)
            self.db.rollback()
        raise ValueError("simple_direction_compatibility_frontier_stale")

    def record_simple_direction_compatibility_query_scopes(
        self,
        *,
        portfolio_id: str,
        expected_request_contract_fingerprint: str,
        remaining_query_scopes: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Freeze the executor-emitted exact scope queue before the first continuation.

        The initial proposal may have executed one page before a compatibility
        attempt exists.  `attemptedThisTurn` is therefore consumed once here;
        later pages are advanced only by reconciled `slotQueryOutcomes`.
        """

        normalized_scopes = SimpleDirectionFrontierService.normalize_remaining_query_scopes(remaining_query_scopes)
        if not normalized_scopes:
            raise ValueError("simple_direction_compatibility_remaining_query_scopes_missing")
        observation_fingerprint = SimpleDirectionFrontierService._fingerprint(normalized_scopes)
        for _attempt in range(4):
            row = self.db.execute(
                """SELECT summary_json, request_contract_fingerprint, source_user_turn_id
                FROM agent_plan_portfolios WHERE id = ?""",
                (portfolio_id,),
            ).fetchone()
            if row is None:
                raise ValueError("plan_portfolio_not_found")
            request_fingerprint = str(row["request_contract_fingerprint"] or "")
            if request_fingerprint != str(expected_request_contract_fingerprint or ""):
                raise ValueError("portfolio_request_fingerprint_mismatch")
            raw_summary = str(row["summary_json"] or "{}")
            try:
                summary = json.loads(raw_summary)
            except (TypeError, ValueError):
                summary = {}
            frozen = summary.get("simpleDirectionCompatibilityFrontier")
            if (
                not isinstance(frozen, dict)
                or str(frozen.get("schemaVersion") or "") != "simple-direction-compatibility-frontier-v1"
                or str(frozen.get("planningRootId") or "") != str(row["source_user_turn_id"] or "")
                or str(frozen.get("requestContractFingerprint") or "") != request_fingerprint
            ):
                raise ValueError("simple_direction_compatibility_frontier_identity_mismatch")
            prior_observation = str(frozen.get("scopeObservationFingerprint") or "")
            if prior_observation:
                if prior_observation != observation_fingerprint:
                    raise ValueError("simple_direction_compatibility_scope_observation_mismatch")
                return copy.deepcopy(frozen)
            attempts = summary.get("simpleDirectionCompatibilityAttempts")
            if any(
                isinstance(item, dict) and str(item.get("status") or "") in {"claimed", "provider_pending"}
                for item in (attempts.values() if isinstance(attempts, dict) else [])
            ):
                raise ValueError("simple_direction_compatibility_claim_in_progress")
            snapshot = SimpleDirectionFrontierService.create(
                planning_root_id=str(row["source_user_turn_id"] or ""),
                request_contract_fingerprint=request_fingerprint,
                evidence={"schemaVersion": "compatibility-slot-frontier-v1", "entities": []},
                locality="",
                max_pages_per_query=max(1, int(frozen.get("maxPagesPerQuery") or 1)),
            )
            snapshot["executionProfile"]["pageOffset"] = max(
                1,
                int(frozen.get("pageOffset") or 1),
            )
            snapshot["remainingQueryScopes"] = copy.deepcopy(normalized_scopes)
            snapshot["remainingQueryScopesAuthoritative"] = True
            for scope in normalized_scopes:
                if scope.get("attemptedThisTurn") is not True:
                    continue
                provider_outcome = str(scope.get("providerOutcome") or "").strip().casefold()
                if provider_outcome not in {"success", "failure"}:
                    continue
                query = SimpleDirectionFrontierService.begin_slot_query(
                    snapshot,
                    day_number=int(scope["dayNumber"]),
                    slot_id=str(scope["slotId"]),
                    day_seed_amap_id=str(scope["daySeedAmapId"]),
                    query_scope_fingerprint=str(scope["queryScopeFingerprint"]),
                )
                snapshot = SimpleDirectionFrontierService.record_slot_query(
                    snapshot,
                    query={**copy.deepcopy(scope), **query},
                    provider_outcome=provider_outcome,
                    admitted_physical_groups=[],
                    rejected_physical_groups=[],
                )
            SimpleDirectionFrontierService._refresh_status(snapshot)
            frozen["slotFrontierSnapshot"] = copy.deepcopy(snapshot)
            frozen["remainingQueryScopes"] = copy.deepcopy(
                SimpleDirectionFrontierService.remaining_query_scopes(snapshot)
            )
            frozen["scopeObservationFingerprint"] = observation_fingerprint
            frozen["frontierStatus"] = str(snapshot.get("frontierStatus") or "poi_exhausted")
            frozen["remainingPoiPageCount"] = int(snapshot.get("remainingPoiPageCount") or 0)
            summary["simpleDirectionCompatibilityFrontier"] = frozen
            updated = self.db.execute(
                """UPDATE agent_plan_portfolios SET summary_json = ?, updated_at = ?
                WHERE id = ? AND summary_json = ?""",
                (self._dump(summary), self._now(), portfolio_id, raw_summary),
            )
            if updated.rowcount == 1:
                self.db.commit()
                return copy.deepcopy(frozen)
            self.db.rollback()
        raise ValueError("simple_direction_compatibility_frontier_stale")

    def claim_simple_direction_frontier_attempt(
        self,
        *,
        portfolio_id: str,
        execution_id: str,
        campus_slots: list[dict[str, Any]],
        expected_request_contract_fingerprint: str,
    ) -> dict[str, Any]:
        """Claim an idempotent campus assignment before any POI Provider call."""

        execution_identity = str(execution_id or "").strip()
        if not execution_identity:
            raise ValueError("simple_direction_frontier_execution_identity_missing")
        for _attempt in range(4):
            row = self.db.execute(
                """SELECT summary_json, request_contract_fingerprint, status
                FROM agent_plan_portfolios WHERE id = ?""",
                (portfolio_id,),
            ).fetchone()
            if row is None:
                raise ValueError("plan_portfolio_not_found")
            request_fingerprint = str(row["request_contract_fingerprint"] or "")
            if request_fingerprint != str(expected_request_contract_fingerprint or ""):
                raise ValueError("portfolio_request_fingerprint_mismatch")
            raw_summary = str(row["summary_json"] or "{}")
            try:
                summary = json.loads(raw_summary)
            except (TypeError, ValueError):
                summary = {}
            frontier = summary.get("simpleDirectionFrontier")
            if not isinstance(frontier, dict):
                raise ValueError("simple_direction_frontier_missing")
            attempts = summary.get("simpleDirectionFrontierAttempts")
            if not isinstance(attempts, dict):
                attempts = {}
                summary["simpleDirectionFrontierAttempts"] = attempts
            existing = attempts.get(execution_identity)
            if isinstance(existing, dict):
                if str(existing.get("requestContractFingerprint") or "") != request_fingerprint:
                    raise ValueError("simple_direction_frontier_attempt_identity_mismatch")
                existing_attempt = existing.get("attempt")
                if not isinstance(existing_attempt, dict):
                    raise ValueError("simple_direction_frontier_attempt_invalid")
                if str(existing.get("status") or "") != "provider_pending":
                    return copy.deepcopy(existing_attempt)
                if any(
                    other_execution_id != execution_identity
                    and isinstance(record, dict)
                    and str(record.get("status") or "") == "claimed"
                    for other_execution_id, record in attempts.items()
                ):
                    raise ValueError("simple_direction_frontier_claim_in_progress")
                # A user-triggered retry reuses the exact same assignment.  It
                # does not advance the qualification cursor or silently switch
                # to another school after a transport failure.
                resumed = copy.deepcopy(existing)
                resumed["status"] = "claimed"
                resumed["claimAttempt"] = int(resumed.get("claimAttempt") or 1) + 1
                resumed["claimedAt"] = self._now()
                attempts[execution_identity] = resumed
                updated = self.db.execute(
                    """UPDATE agent_plan_portfolios SET summary_json = ?, updated_at = ?
                    WHERE id = ? AND summary_json = ?""",
                    (self._dump(summary), self._now(), portfolio_id, raw_summary),
                )
                if updated.rowcount == 1:
                    self.db.commit()
                    return copy.deepcopy(existing_attempt)
                self.db.rollback()
                continue
            if any(
                isinstance(record, dict) and str(record.get("status") or "") == "provider_pending"
                for record in attempts.values()
            ):
                raise ValueError("simple_direction_frontier_provider_recovery_required")
            if any(
                isinstance(record, dict) and str(record.get("status") or "") == "claimed"
                for record in attempts.values()
            ):
                # A comparison root has one bounded Provider budget and one
                # monotonic cursor.  Two different continuation executions may
                # not concurrently claim disjoint entity pairs from the same
                # frozen frontier.
                raise ValueError("simple_direction_frontier_claim_in_progress")
            if str(frontier.get("frontierStatus") or "") != "has_more":
                raise ValueError("simple_direction_frontier_not_available")
            claimed_entities = {
                str(assignment.get("evidenceEntityFingerprint") or "")
                for record in attempts.values()
                if isinstance(record, dict) and str(record.get("status") or "") == "claimed"
                for attempt_material in [record.get("attempt")]
                if isinstance(attempt_material, dict)
                for assignment in attempt_material.get("campusAssignments") or []
                if isinstance(assignment, dict) and assignment.get("isNewQualificationEntity") is True
            }
            attempt_material = SimpleDirectionFrontierService.begin_attempt(
                frontier,
                campus_slots=campus_slots,
                excluded_entity_fingerprints=claimed_entities,
            )
            self._bind_simple_direction_qualification_entities(
                attempt=attempt_material,
                frontier=frontier,
                summary=summary,
                planning_root_id=str(
                    summary.get("planningSelectionRootTurnId") or frontier.get("planningRootId") or ""
                ),
                request_contract_fingerprint=request_fingerprint,
            )
            # The executor receives an immutable copy of the server-owned slot
            # cursor state.  Once a campus is grounded it can derive the exact
            # day-seed-scoped meal/park page without consulting client input.
            # The authoritative cursor is advanced only during reconciliation.
            attempt_material["slotFrontierSnapshot"] = {
                "schemaVersion": str(frontier.get("schemaVersion") or ""),
                "requestContractFingerprint": request_fingerprint,
                "executionProfile": copy.deepcopy(frontier.get("executionProfile") or {}),
                "slotFrontiers": copy.deepcopy(frontier.get("slotFrontiers") or {}),
                "remainingQueryScopes": copy.deepcopy(frontier.get("remainingQueryScopes") or []),
                "remainingQueryScopesAuthoritative": (frontier.get("remainingQueryScopesAuthoritative") is True),
            }
            attempt_material["executionId"] = execution_identity
            attempt_material["requestContractFingerprint"] = request_fingerprint
            attempt_material["attemptFingerprint"] = SimpleDirectionFrontierService._fingerprint(
                {key: value for key, value in attempt_material.items() if key != "attemptFingerprint"}
            )
            attempts[execution_identity] = {
                "schemaVersion": "simple-direction-frontier-claim-v1",
                "executionId": execution_identity,
                "requestContractFingerprint": request_fingerprint,
                "status": "claimed",
                "claimAttempt": 1,
                "claimedAt": self._now(),
                "attempt": copy.deepcopy(attempt_material),
            }
            updated = self.db.execute(
                """UPDATE agent_plan_portfolios SET summary_json = ?, updated_at = ?
                WHERE id = ? AND summary_json = ?""",
                (self._dump(summary), self._now(), portfolio_id, raw_summary),
            )
            if updated.rowcount == 1:
                self.db.commit()
                return attempt_material
            self.db.rollback()
        raise ValueError("simple_direction_frontier_stale")

    def claim_simple_direction_compatibility_attempt(
        self,
        *,
        portfolio_id: str,
        execution_id: str,
        source_assistant_turn_id: str,
        request_turn_id: str,
        choice_id: str,
        expected_request_contract_fingerprint: str,
        max_pages_per_query: int = 3,
        slot_query_scopes: Optional[list[dict[str, Any]]] = None,
        defer_slot_scope_claim: bool = False,
    ) -> dict[str, Any]:
        """Persist a server-owned paged attempt for a generic continuation.

        Some Simple Direction roots are created from ordinary required POI
        intents and therefore have no qualification-entity frontier.  Their
        bounded continuation capability is still an opaque server choice and
        needs the same durable claim/reconcile lifecycle as a qualification
        attempt.  Page 1 belongs to the initial proposal.  Generic
        compatibility continuations consume the same preconfigured page
        frontier from page 2 onward; the client never supplies or advances
        this cursor.  A deferred current-anchor continuation is different:
        its exact scope does not exist until the new campus has been grounded,
        so the late-bound scope starts at page 1 independently of how many
        prior directions exist.
        """

        execution_identity = str(execution_id or "").strip()
        source_turn_identity = str(source_assistant_turn_id or "").strip()
        request_turn_identity = str(request_turn_id or "").strip()
        choice_identity = str(choice_id or "").strip()
        if not all((execution_identity, source_turn_identity, request_turn_identity, choice_identity)):
            raise ValueError("simple_direction_compatibility_attempt_identity_missing")
        for _attempt in range(4):
            row = self.db.execute(
                """SELECT session_id, source_user_turn_id, source_assistant_turn_id,
                          summary_json, request_contract_fingerprint, status
                FROM agent_plan_portfolios WHERE id = ?""",
                (portfolio_id,),
            ).fetchone()
            if row is None:
                raise ValueError("plan_portfolio_not_found")
            request_fingerprint = str(row["request_contract_fingerprint"] or "")
            if request_fingerprint != str(expected_request_contract_fingerprint or ""):
                raise ValueError("portfolio_request_fingerprint_mismatch")
            if str(row["status"] or "") != "awaiting_selection":
                raise ValueError("simple_direction_compatibility_portfolio_not_available")
            raw_summary = str(row["summary_json"] or "{}")
            try:
                summary = json.loads(raw_summary)
            except (TypeError, ValueError):
                summary = {}
            if isinstance(summary.get("simpleDirectionFrontier"), dict) and summary.get("simpleDirectionFrontier"):
                raise ValueError("simple_direction_compatibility_frontier_present")
            frozen_frontier = summary.get("simpleDirectionCompatibilityFrontier")
            if not isinstance(frozen_frontier, dict):
                raise ValueError("simple_direction_compatibility_frontier_missing")
            if (
                str(frozen_frontier.get("schemaVersion") or "") != "simple-direction-compatibility-frontier-v1"
                or str(frozen_frontier.get("planningRootId") or "") != str(row["source_user_turn_id"] or "")
                or str(frozen_frontier.get("requestContractFingerprint") or "") != request_fingerprint
            ):
                raise ValueError("simple_direction_compatibility_frontier_identity_mismatch")
            normalized_max_pages = max(1, int(frozen_frontier.get("maxPagesPerQuery") or 1))
            normalized_page_offset = max(1, int(frozen_frontier.get("pageOffset") or 1))

            execution = self.db.execute(
                """SELECT *
                FROM agent_choice_executions WHERE id = ?""",
                (execution_identity,),
            ).fetchone()
            if (
                execution is None
                or str(execution["session_id"] or "") != str(row["session_id"] or "")
                or str(execution["source_turn_id"] or "") != source_turn_identity
                or str(execution["source_user_turn_id"] or "") != str(row["source_user_turn_id"] or "")
                or str(execution["request_turn_id"] or "") != request_turn_identity
                or str(execution["choice_id"] or "") != choice_identity
                or str(execution["action"] or "") != "continue_plan_expansion"
                or str(execution["status"] or "") != "executing"
            ):
                raise ValueError("simple_direction_compatibility_execution_identity_mismatch")
            source_turn = self.db.execute(
                """SELECT agent_response_json FROM conversation_turns
                WHERE id = ? AND session_id = ? AND role = 'assistant' AND status != 'superseded'""",
                (source_turn_identity, str(row["session_id"] or "")),
            ).fetchone()
            try:
                source_payload = json.loads(str(source_turn["agent_response_json"] or "{}")) if source_turn else {}
            except (TypeError, ValueError):
                source_payload = {}
            source_option = next(
                (
                    item
                    for item in source_payload.get("choiceOptions") or []
                    if isinstance(item, dict) and str(item.get("id") or "") == choice_identity
                ),
                None,
            )
            from src.services.conversation_operation_identity import ConversationOperationIdentity
            try:
                source_option = ConversationOperationIdentity(self.db).source_option_for_execution(dict(execution))
            except (ValueError, KeyError) as error:
                raise ValueError("simple_direction_compatibility_source_choice_mismatch") from error
            if (
                not isinstance(source_option, dict)
                or str(source_option.get("action") or "") != "continue_plan_expansion"
                or str(source_option.get("kind") or "") != "simple_direction_more_plans"
                or str(source_option.get("sourceAssistantTurnId") or "") != source_turn_identity
                or str(source_option.get("planningSelectionRootTurnId") or "") != str(row["source_user_turn_id"] or "")
                or str(source_option.get("rootPortfolioId") or "") != str(portfolio_id)
                or str(source_option.get("requestContractFingerprint") or "") != request_fingerprint
            ):
                raise ValueError("simple_direction_compatibility_source_choice_mismatch")

            attempts = summary.get("simpleDirectionCompatibilityAttempts")
            if not isinstance(attempts, dict):
                attempts = {}
                summary["simpleDirectionCompatibilityAttempts"] = attempts
            existing = attempts.get(execution_identity)
            if isinstance(existing, dict):
                if any(
                    str(existing.get(key) or "") != expected
                    for key, expected in (
                        ("executionId", execution_identity),
                        ("sourceAssistantTurnId", source_turn_identity),
                        ("requestTurnId", request_turn_identity),
                        ("choiceId", choice_identity),
                        ("requestContractFingerprint", request_fingerprint),
                    )
                ) or str(
                    existing.get("attemptFingerprint") or ""
                ) != self.simple_direction_compatibility_attempt_fingerprint(existing):
                    raise ValueError("simple_direction_compatibility_attempt_identity_mismatch")
                status = str(existing.get("status") or "")
                if status == "provider_pending":
                    resumed = copy.deepcopy(existing)
                    resumed["status"] = "claimed"
                    resumed["claimAttempt"] = int(resumed.get("claimAttempt") or 1) + 1
                    resumed["claimedAt"] = self._now()
                    resumed["updatedAt"] = self._now()
                    attempts[execution_identity] = resumed
                    updated = self.db.execute(
                        """UPDATE agent_plan_portfolios SET summary_json = ?, updated_at = ?
                        WHERE id = ? AND summary_json = ?""",
                        (self._dump(summary), self._now(), portfolio_id, raw_summary),
                    )
                    if updated.rowcount == 1:
                        self.db.commit()
                        return copy.deepcopy(resumed)
                    self.db.rollback()
                    continue
                return copy.deepcopy(existing)
            if any(
                isinstance(record, dict) and str(record.get("status") or "") == "claimed"
                for record in attempts.values()
            ):
                raise ValueError("simple_direction_compatibility_claim_in_progress")
            latest = max(
                (record for record in attempts.values() if isinstance(record, dict)),
                key=lambda record: str(record.get("updatedAt") or record.get("claimedAt") or ""),
                default={},
            )
            if (
                latest
                and str(latest.get("status") or "") in {"reconciled", "no_progress"}
                and str(latest.get("frontierStatus") or "") != "has_more"
            ):
                raise ValueError("simple_direction_compatibility_frontier_not_available")
            if defer_slot_scope_claim and slot_query_scopes:
                raise ValueError("simple_direction_compatibility_deferred_scope_conflict")
            supplied_slot_scopes = (
                []
                if defer_slot_scope_claim
                else (
                    slot_query_scopes
                    if slot_query_scopes is not None
                    else frozen_frontier.get("remainingQueryScopes") or []
                )
            )
            normalized_slot_scopes = SimpleDirectionFrontierService.normalize_remaining_query_scopes(
                supplied_slot_scopes
            )
            slot_queries: dict[str, dict[str, Any]] = {}
            slot_frontier_snapshot: dict[str, Any] = {}
            stored_snapshot = frozen_frontier.get("slotFrontierSnapshot")
            if not defer_slot_scope_claim and (
                normalized_slot_scopes or isinstance(stored_snapshot, dict)
            ):
                if isinstance(stored_snapshot, dict):
                    slot_frontier_snapshot = copy.deepcopy(stored_snapshot)
                else:
                    slot_frontier_snapshot = SimpleDirectionFrontierService.create(
                        planning_root_id=str(row["source_user_turn_id"] or ""),
                        request_contract_fingerprint=request_fingerprint,
                        evidence={"schemaVersion": "compatibility-slot-frontier-v1", "entities": []},
                        locality="",
                        max_pages_per_query=normalized_max_pages,
                    )
                    slot_frontier_snapshot["executionProfile"]["pageOffset"] = normalized_page_offset
                    slot_frontier_snapshot["executionProfile"]["profileFingerprint"] = hashlib.sha256(
                        self._dump(
                            {
                                "maxPagesPerQuery": normalized_max_pages,
                                "pageOffset": normalized_page_offset,
                            }
                        ).encode("utf-8")
                    ).hexdigest()
                    slot_frontier_snapshot["frontierFingerprint"] = (
                        SimpleDirectionFrontierService._frontier_fingerprint(slot_frontier_snapshot)
                    )
                if slot_query_scopes is not None or normalized_slot_scopes:
                    slot_frontier_snapshot["remainingQueryScopes"] = copy.deepcopy(normalized_slot_scopes)
                    slot_frontier_snapshot["remainingQueryScopesAuthoritative"] = True
                for scope in normalized_slot_scopes:
                    if scope.get("initialPageAlreadyAttempted") is not True:
                        continue
                    query = SimpleDirectionFrontierService.begin_slot_query(
                        slot_frontier_snapshot,
                        day_number=int(scope["dayNumber"]),
                        slot_id=str(scope["slotId"]),
                        day_seed_amap_id=str(scope["daySeedAmapId"]),
                        query_scope_fingerprint=str(scope["queryScopeFingerprint"]),
                    )
                    if int(query.get("page") or 1) == 1 and query.get("exhausted") is not True:
                        query = {**copy.deepcopy(scope), **query}
                        slot_frontier_snapshot = SimpleDirectionFrontierService.record_slot_query(
                            slot_frontier_snapshot,
                            query=query,
                            provider_outcome="success",
                            admitted_physical_groups=[],
                            rejected_physical_groups=[],
                        )
                slot_queries = SimpleDirectionFrontierService.claim_slot_queries(slot_frontier_snapshot)
                if not slot_queries:
                    raise ValueError("simple_direction_compatibility_frontier_not_available")
                frozen_frontier["slotFrontierSnapshot"] = copy.deepcopy(slot_frontier_snapshot)
                frozen_frontier["remainingQueryScopes"] = copy.deepcopy(
                    SimpleDirectionFrontierService.remaining_query_scopes(slot_frontier_snapshot)
                )
                summary["simpleDirectionCompatibilityFrontier"] = frozen_frontier
                provider_page = min(int(item.get("page") or 1) for item in slot_queries.values())
            else:
                if defer_slot_scope_claim:
                    provider_page = 1
                else:
                    consumed_attempt_count = sum(
                        isinstance(record, dict)
                        and str(record.get("status") or "") in {"reconciled", "no_progress", "failed_terminal"}
                        for record in attempts.values()
                    )
                    provider_page = 2 + consumed_attempt_count
                    if provider_page > normalized_max_pages:
                        raise ValueError("simple_direction_compatibility_frontier_not_available")
            identity_material = {
                "schemaVersion": (
                    "simple-direction-compatibility-attempt-v3"
                    if slot_queries
                    else "simple-direction-compatibility-attempt-v2"
                ),
                "executionId": execution_identity,
                "sessionId": str(row["session_id"] or ""),
                "planningSelectionRootTurnId": str(row["source_user_turn_id"] or ""),
                "rootPortfolioId": str(portfolio_id),
                "sourceAssistantTurnId": source_turn_identity,
                "requestTurnId": request_turn_identity,
                "choiceId": choice_identity,
                "requestContractFingerprint": request_fingerprint,
                "providerPage": provider_page,
                "pageOffset": normalized_page_offset,
                "maxPagesPerQuery": normalized_max_pages,
                **(
                    {
                        "slotQueries": copy.deepcopy(slot_queries),
                        "slotFrontierSnapshot": copy.deepcopy(slot_frontier_snapshot),
                        "remainingQueryScopes": copy.deepcopy(frozen_frontier.get("remainingQueryScopes") or []),
                    }
                    if slot_queries
                    else {}
                ),
            }
            attempt_fingerprint = self.simple_direction_compatibility_attempt_fingerprint(identity_material)
            record = {
                **identity_material,
                "attemptFingerprint": attempt_fingerprint,
                "status": "claimed",
                "claimAttempt": 1,
                "claimedAt": self._now(),
                "updatedAt": self._now(),
            }
            attempts[execution_identity] = record
            updated = self.db.execute(
                """UPDATE agent_plan_portfolios SET summary_json = ?, updated_at = ?
                WHERE id = ? AND summary_json = ?""",
                (self._dump(summary), self._now(), portfolio_id, raw_summary),
            )
            if updated.rowcount == 1:
                self.db.commit()
                return copy.deepcopy(record)
            self.db.rollback()
        raise ValueError("simple_direction_compatibility_attempt_stale")

    def attach_simple_direction_compatibility_slot_scopes(
        self,
        *,
        portfolio_id: str,
        execution_id: str,
        expected_request_contract_fingerprint: str,
        slot_query_scopes: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Late-bind current-anchor exact scopes to one claimed attempt.

        A compatibility continuation is claimed before its campus candidate is
        grounded, so its adjacent meal/park scopes do not yet exist.  The
        executor calls this boundary after grounding that campus and before
        making any nearby Provider request.  The first attachment replaces the
        frozen authoritative queue while retaining historical per-scope cursor
        rows; subsequent attachments may add a new slot, but an already-bound
        slot is immutable.  No Provider outcome or cursor advancement occurs
        here.
        """

        execution_identity = str(execution_id or "").strip()
        if not execution_identity:
            raise ValueError("simple_direction_compatibility_execution_missing")
        normalized_scopes = SimpleDirectionFrontierService.normalize_remaining_query_scopes(
            slot_query_scopes
        )
        if not normalized_scopes:
            raise ValueError("simple_direction_compatibility_slot_scopes_missing")
        incoming_by_slot: dict[str, list[dict[str, Any]]] = {}
        for scope in normalized_scopes:
            incoming_by_slot.setdefault(str(scope["slotId"]), []).append(scope)

        for _attempt in range(4):
            row = self.db.execute(
                """SELECT source_user_turn_id, summary_json,
                          request_contract_fingerprint, status
                FROM agent_plan_portfolios WHERE id = ?""",
                (portfolio_id,),
            ).fetchone()
            if row is None:
                raise ValueError("plan_portfolio_not_found")
            request_fingerprint = str(row["request_contract_fingerprint"] or "")
            if request_fingerprint != str(expected_request_contract_fingerprint or ""):
                raise ValueError("portfolio_request_fingerprint_mismatch")
            if str(row["status"] or "") != "awaiting_selection":
                raise ValueError("simple_direction_compatibility_portfolio_not_available")
            raw_summary = str(row["summary_json"] or "{}")
            try:
                summary = json.loads(raw_summary)
            except (TypeError, ValueError):
                summary = {}

            frozen_frontier = summary.get("simpleDirectionCompatibilityFrontier")
            if (
                not isinstance(frozen_frontier, dict)
                or str(frozen_frontier.get("schemaVersion") or "")
                != "simple-direction-compatibility-frontier-v1"
                or str(frozen_frontier.get("planningRootId") or "")
                != str(row["source_user_turn_id"] or "")
                or str(frozen_frontier.get("requestContractFingerprint") or "")
                != request_fingerprint
            ):
                raise ValueError("simple_direction_compatibility_frontier_identity_mismatch")
            attempts = summary.get("simpleDirectionCompatibilityAttempts")
            record = attempts.get(execution_identity) if isinstance(attempts, dict) else None
            if not isinstance(record, dict):
                raise ValueError("simple_direction_compatibility_attempt_missing")
            if (
                str(record.get("executionId") or "") != execution_identity
                or str(record.get("rootPortfolioId") or "") != str(portfolio_id)
                or str(record.get("planningSelectionRootTurnId") or "")
                != str(row["source_user_turn_id"] or "")
                or str(record.get("requestContractFingerprint") or "")
                != request_fingerprint
                or str(record.get("attemptFingerprint") or "")
                != self.simple_direction_compatibility_attempt_fingerprint(record)
            ):
                raise ValueError("simple_direction_compatibility_attempt_identity_mismatch")
            if str(record.get("status") or "") != "claimed":
                raise ValueError("simple_direction_compatibility_attempt_not_claimed")
            if str(record.get("schemaVersion") or "") not in {
                "simple-direction-compatibility-attempt-v2",
                "simple-direction-compatibility-attempt-v3",
            }:
                raise ValueError("simple_direction_compatibility_attempt_identity_mismatch")

            record_snapshot = record.get("slotFrontierSnapshot")
            existing_scopes = SimpleDirectionFrontierService.normalize_remaining_query_scopes(
                record.get("remainingQueryScopes") or []
            )
            if isinstance(record_snapshot, dict):
                snapshot_scopes = SimpleDirectionFrontierService.normalize_remaining_query_scopes(
                    record_snapshot.get("remainingQueryScopes") or []
                )
                if snapshot_scopes != existing_scopes:
                    raise ValueError("simple_direction_compatibility_attempt_identity_mismatch")
            existing_by_slot: dict[str, list[dict[str, Any]]] = {}
            for scope in existing_scopes:
                existing_by_slot.setdefault(str(scope["slotId"]), []).append(scope)
            for slot_id, incoming in incoming_by_slot.items():
                existing = existing_by_slot.get(slot_id)
                if existing is not None and existing != incoming:
                    raise ValueError("simple_direction_compatibility_slot_scope_conflict")

            merged_by_slot = copy.deepcopy(existing_by_slot)
            added_slot = False
            for slot_id, incoming in incoming_by_slot.items():
                if slot_id not in merged_by_slot:
                    merged_by_slot[slot_id] = copy.deepcopy(incoming)
                    added_slot = True
            if not added_slot:
                if isinstance(record_snapshot, dict):
                    frozen_snapshot = frozen_frontier.get("slotFrontierSnapshot")
                    if (
                        not isinstance(frozen_snapshot, dict)
                        or frozen_snapshot != record_snapshot
                        or SimpleDirectionFrontierService.normalize_remaining_query_scopes(
                            frozen_frontier.get("remainingQueryScopes") or []
                        )
                        != existing_scopes
                    ):
                        raise ValueError("simple_direction_compatibility_frontier_identity_mismatch")
                return copy.deepcopy(record)

            merged_scopes = SimpleDirectionFrontierService.normalize_remaining_query_scopes(
                scope for scopes in merged_by_slot.values() for scope in scopes
            )
            if isinstance(record_snapshot, dict):
                slot_frontier_snapshot = copy.deepcopy(record_snapshot)
                frozen_snapshot = frozen_frontier.get("slotFrontierSnapshot")
                if not isinstance(frozen_snapshot, dict) or frozen_snapshot != record_snapshot:
                    raise ValueError("simple_direction_compatibility_frontier_identity_mismatch")
            elif isinstance(frozen_frontier.get("slotFrontierSnapshot"), dict):
                # Retain historical exact-scope cursors, but replace their
                # authoritative queue with scopes bound to this execution.
                slot_frontier_snapshot = copy.deepcopy(frozen_frontier["slotFrontierSnapshot"])
            else:
                slot_frontier_snapshot = SimpleDirectionFrontierService.create(
                    planning_root_id=str(row["source_user_turn_id"] or ""),
                    request_contract_fingerprint=request_fingerprint,
                    evidence={"schemaVersion": "compatibility-slot-frontier-v1", "entities": []},
                    locality="",
                    max_pages_per_query=max(1, int(record.get("maxPagesPerQuery") or 1)),
                )
            profile = slot_frontier_snapshot.get("executionProfile")
            if not isinstance(profile, dict):
                raise ValueError("simple_direction_compatibility_frontier_identity_mismatch")
            maximum_pages = max(1, int(record.get("maxPagesPerQuery") or 1))
            page_offset = max(1, int(record.get("pageOffset") or 1))
            if max(1, int(profile.get("maxPagesPerQuery") or 1)) != maximum_pages:
                raise ValueError("simple_direction_compatibility_frontier_identity_mismatch")
            profile["pageOffset"] = page_offset
            profile["profileFingerprint"] = hashlib.sha256(
                self._dump(
                    {
                        "maxPagesPerQuery": maximum_pages,
                        "pageOffset": page_offset,
                    }
                ).encode("utf-8")
            ).hexdigest()
            slot_frontier_snapshot["remainingQueryScopes"] = copy.deepcopy(merged_scopes)
            slot_frontier_snapshot["remainingQueryScopesAuthoritative"] = True
            SimpleDirectionFrontierService._refresh_status(slot_frontier_snapshot)
            slot_queries = SimpleDirectionFrontierService.claim_slot_queries(slot_frontier_snapshot)
            if set(slot_queries) != set(merged_by_slot) or any(
                item.get("exhausted") is not False
                or int(item.get("page") or 0) <= 0
                or int(item.get("offset") or 0) <= 0
                for item in slot_queries.values()
            ):
                raise ValueError("simple_direction_compatibility_frontier_not_available")
            remaining_scopes = SimpleDirectionFrontierService.remaining_query_scopes(
                slot_frontier_snapshot
            )

            next_record = copy.deepcopy(record)
            next_record.update(
                {
                    "schemaVersion": "simple-direction-compatibility-attempt-v3",
                    "providerPage": min(int(item.get("page") or 1) for item in slot_queries.values()),
                    "slotQueries": copy.deepcopy(slot_queries),
                    "slotFrontierSnapshot": copy.deepcopy(slot_frontier_snapshot),
                    "remainingQueryScopes": copy.deepcopy(remaining_scopes),
                    "updatedAt": self._now(),
                }
            )
            next_record["attemptFingerprint"] = self.simple_direction_compatibility_attempt_fingerprint(
                next_record
            )
            attempts[execution_identity] = next_record
            frozen_frontier["slotFrontierSnapshot"] = copy.deepcopy(slot_frontier_snapshot)
            frozen_frontier["remainingQueryScopes"] = copy.deepcopy(remaining_scopes)
            frozen_frontier["frontierStatus"] = str(
                slot_frontier_snapshot.get("frontierStatus") or "poi_exhausted"
            )
            frozen_frontier["remainingPoiPageCount"] = int(
                slot_frontier_snapshot.get("remainingPoiPageCount") or 0
            )
            summary["simpleDirectionCompatibilityFrontier"] = frozen_frontier
            updated = self.db.execute(
                """UPDATE agent_plan_portfolios SET summary_json = ?, updated_at = ?
                WHERE id = ? AND summary_json = ?""",
                (self._dump(summary), self._now(), portfolio_id, raw_summary),
            )
            if updated.rowcount == 1:
                self.db.commit()
                return copy.deepcopy(next_record)
            self.db.rollback()
        raise ValueError("simple_direction_compatibility_attempt_stale")

    def reconcile_simple_direction_compatibility_attempt(
        self,
        *,
        portfolio_id: str,
        execution_id: str,
        result_assistant_turn_id: str,
        proposal_id: Optional[str],
        proposal_delta: int,
        disposition: str,
        expected_request_contract_fingerprint: str,
        frontier_status: str = "",
        reason_code: str = "",
        result_response_payload: Optional[dict[str, Any]] = None,
        slot_query_outcomes: Optional[list[dict[str, Any]]] = None,
        route_progress: Optional[dict[str, Any]] = None,
        remaining_query_scopes: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        """Settle one compatibility attempt without inventing a Provider frontier.

        When a result payload is supplied, persist the minimum authoritative
        assistant evidence in the same transaction as the attempt settlement.
        This closes the crash window between consuming a bounded attempt and
        making its zero-write result independently recoverable.
        """

        execution_identity = str(execution_id or "").strip()
        result_turn_identity = str(result_assistant_turn_id or "").strip()
        normalized_delta = int(proposal_delta or 0)
        if not execution_identity or not result_turn_identity or normalized_delta not in {0, 1}:
            raise ValueError("simple_direction_compatibility_result_invalid")
        if normalized_delta == 1 and not str(proposal_id or ""):
            raise ValueError("simple_direction_compatibility_proposal_missing")
        if normalized_delta == 0 and str(proposal_id or ""):
            raise ValueError("simple_direction_compatibility_proposal_unexpected")
        normalized_disposition = str(disposition or "")
        normalized_reason_code = str(reason_code or "")
        strict_progress_evidence = slot_query_outcomes is not None or route_progress is not None
        normalized_frontier_status = (
            "provider_pending"
            if normalized_disposition == "provider_pending"
            else str(frontier_status or "") or ("has_more" if normalized_delta == 1 else "poi_exhausted")
        )
        if normalized_frontier_status not in {"has_more", "poi_exhausted", "provider_pending"}:
            raise ValueError("simple_direction_compatibility_frontier_status_invalid")
        reconcile_input_fingerprint = hashlib.sha256(
            self._dump(
                {
                    "resultAssistantTurnId": result_turn_identity,
                    "proposalId": str(proposal_id or "") or None,
                    "proposalDelta": normalized_delta,
                    "disposition": normalized_disposition,
                    "frontierStatus": str(frontier_status or ""),
                    "reasonCode": normalized_reason_code,
                    "slotQueryOutcomes": slot_query_outcomes,
                    "routeProgress": route_progress,
                    "remainingQueryScopes": remaining_query_scopes,
                }
            ).encode("utf-8")
        ).hexdigest()
        for _attempt in range(4):
            row = self.db.execute(
                """SELECT session_id, source_user_turn_id, summary_json,
                          request_contract_fingerprint
                FROM agent_plan_portfolios WHERE id = ?""",
                (portfolio_id,),
            ).fetchone()
            if row is None:
                raise ValueError("plan_portfolio_not_found")
            request_fingerprint = str(row["request_contract_fingerprint"] or "")
            if request_fingerprint != str(expected_request_contract_fingerprint or ""):
                raise ValueError("portfolio_request_fingerprint_mismatch")
            raw_summary = str(row["summary_json"] or "{}")
            try:
                summary = json.loads(raw_summary)
            except (TypeError, ValueError):
                summary = {}
            attempts = summary.get("simpleDirectionCompatibilityAttempts")
            record = attempts.get(execution_identity) if isinstance(attempts, dict) else None
            if not isinstance(record, dict):
                raise ValueError("simple_direction_compatibility_attempt_missing")
            if (
                str(record.get("schemaVersion") or "")
                not in {
                    "simple-direction-compatibility-attempt-v1",
                    "simple-direction-compatibility-attempt-v2",
                    "simple-direction-compatibility-attempt-v3",
                }
                or str(record.get("executionId") or "") != execution_identity
                or str(record.get("planningSelectionRootTurnId") or "") != str(row["source_user_turn_id"] or "")
                or str(record.get("rootPortfolioId") or "") != str(portfolio_id)
                or str(record.get("requestContractFingerprint") or "") != request_fingerprint
                or str(record.get("attemptFingerprint") or "")
                != self.simple_direction_compatibility_attempt_fingerprint(record)
            ):
                raise ValueError("simple_direction_compatibility_attempt_identity_mismatch")
            if str(record.get("status") or "") in {"reconciled", "no_progress"}:
                stored_input_fingerprint = str(record.get("reconcileInputFingerprint") or "")
                stored_result_fingerprint = str(record.get("reconcileResultFingerprint") or "")
                if stored_input_fingerprint:
                    if (
                        stored_input_fingerprint != reconcile_input_fingerprint
                        or not stored_result_fingerprint
                        or stored_result_fingerprint != self.simple_direction_compatibility_result_fingerprint(record)
                    ):
                        raise ValueError("simple_direction_compatibility_reconcile_replay_mismatch")
                    return copy.deepcopy(record)
                if any(
                    (
                        str(record.get("resultAssistantTurnId") or "") != result_turn_identity,
                        str(record.get("proposalId") or "") != str(proposal_id or ""),
                        int(record.get("proposalDelta") or 0) != normalized_delta,
                        str(record.get("disposition") or "") != normalized_disposition,
                        str(record.get("frontierStatus") or "") != normalized_frontier_status,
                        str(record.get("reasonCode") or "") != normalized_reason_code,
                    )
                ):
                    raise ValueError("simple_direction_compatibility_reconcile_replay_mismatch")
                return copy.deepcopy(record)
            query_progress = False
            updated_slot_snapshot: Optional[dict[str, Any]] = None
            if slot_query_outcomes is not None:
                frozen_compatibility_frontier = summary.get("simpleDirectionCompatibilityFrontier")
                frozen_compatibility_frontier = (
                    frozen_compatibility_frontier if isinstance(frozen_compatibility_frontier, dict) else {}
                )
                stored_snapshot = frozen_compatibility_frontier.get("slotFrontierSnapshot")
                if isinstance(stored_snapshot, dict):
                    updated_slot_snapshot = copy.deepcopy(stored_snapshot)
                elif str(record.get("schemaVersion") or "") == "simple-direction-compatibility-attempt-v3":
                    claimed_snapshot = record.get("slotFrontierSnapshot")
                    updated_slot_snapshot = (
                        copy.deepcopy(claimed_snapshot) if isinstance(claimed_snapshot, dict) else None
                    )
                if updated_slot_snapshot is None and (bool(slot_query_outcomes) or remaining_query_scopes is not None):
                    updated_slot_snapshot = SimpleDirectionFrontierService.create(
                        planning_root_id=str(row["source_user_turn_id"] or ""),
                        request_contract_fingerprint=request_fingerprint,
                        evidence={"schemaVersion": "compatibility-slot-frontier-v1", "entities": []},
                        locality="",
                        max_pages_per_query=max(1, int(record.get("maxPagesPerQuery") or 1)),
                    )
                    page_offset = max(1, int(record.get("pageOffset") or 1))
                    updated_slot_snapshot["executionProfile"]["pageOffset"] = page_offset
                    updated_slot_snapshot["executionProfile"]["profileFingerprint"] = hashlib.sha256(
                        self._dump(
                            {
                                "maxPagesPerQuery": max(
                                    1,
                                    int(record.get("maxPagesPerQuery") or 1),
                                ),
                                "pageOffset": page_offset,
                            }
                        ).encode("utf-8")
                    ).hexdigest()
                    seed_scopes = [
                        copy.deepcopy(item) for item in remaining_query_scopes or [] if isinstance(item, dict)
                    ]
                    seeded_identities = {
                        (
                            int(item.get("dayNumber") or 0),
                            str(item.get("slotId") or ""),
                            str(item.get("daySeedAmapId") or ""),
                            str(item.get("queryScopeFingerprint") or ""),
                        )
                        for item in seed_scopes
                    }
                    for outcome in slot_query_outcomes:
                        if not isinstance(outcome, dict):
                            continue
                        query = outcome.get("query") if isinstance(outcome.get("query"), dict) else outcome
                        identity = (
                            int(query.get("dayNumber") or 0),
                            str(query.get("slotId") or ""),
                            str(query.get("daySeedAmapId") or ""),
                            str(query.get("queryScopeFingerprint") or ""),
                        )
                        if identity not in seeded_identities:
                            seed_scopes.append(copy.deepcopy(query))
                            seeded_identities.add(identity)
                    updated_slot_snapshot["remainingQueryScopes"] = (
                        SimpleDirectionFrontierService.normalize_remaining_query_scopes(seed_scopes)
                    )
                    updated_slot_snapshot["remainingQueryScopesAuthoritative"] = True
                    updated_slot_snapshot["frontierFingerprint"] = SimpleDirectionFrontierService._frontier_fingerprint(
                        updated_slot_snapshot
                    )
                if updated_slot_snapshot is not None and remaining_query_scopes is not None:
                    updated_slot_snapshot["remainingQueryScopes"] = (
                        SimpleDirectionFrontierService.normalize_remaining_query_scopes(remaining_query_scopes)
                    )
                    updated_slot_snapshot["remainingQueryScopesAuthoritative"] = True
                for outcome in slot_query_outcomes:
                    if not isinstance(outcome, dict) or updated_slot_snapshot is None:
                        raise ValueError("simple_direction_compatibility_slot_outcome_invalid")
                    query = outcome.get("query") if isinstance(outcome.get("query"), dict) else outcome
                    expected_query = SimpleDirectionFrontierService.begin_slot_query(
                        updated_slot_snapshot,
                        day_number=int(query.get("dayNumber") or 0),
                        slot_id=str(query.get("slotId") or ""),
                        day_seed_amap_id=str(query.get("daySeedAmapId") or ""),
                        query_scope_fingerprint=str(query.get("queryScopeFingerprint") or ""),
                    )
                    if any(
                        str(query.get(field) if query.get(field) is not None else "")
                        != str(expected_query.get(field) if expected_query.get(field) is not None else "")
                        for field in (
                            "slotFrontierKey",
                            "queryFingerprint",
                            "dayNumber",
                            "slotId",
                            "daySeedAmapId",
                            "queryScopeFingerprint",
                            "page",
                            "offset",
                        )
                    ):
                        raise ValueError("simple_direction_compatibility_slot_outcome_identity_mismatch")
                    provider_outcome = str(outcome.get("providerOutcome") or "").strip().casefold()
                    if provider_outcome in {"success", "rejected"}:
                        query_progress = True
                    updated_slot_snapshot = SimpleDirectionFrontierService.record_slot_query(
                        updated_slot_snapshot,
                        query=query,
                        provider_outcome=provider_outcome,
                        admitted_physical_groups=outcome.get("admittedPhysicalGroups") or [],
                        rejected_physical_groups=outcome.get("rejectedPhysicalGroups") or [],
                    )
                if updated_slot_snapshot is not None:
                    frozen_compatibility_frontier["slotFrontierSnapshot"] = copy.deepcopy(updated_slot_snapshot)
                    frozen_compatibility_frontier["remainingQueryScopes"] = copy.deepcopy(
                        SimpleDirectionFrontierService.remaining_query_scopes(updated_slot_snapshot)
                    )
                    summary["simpleDirectionCompatibilityFrontier"] = frozen_compatibility_frontier
            candidate_progress = normalized_delta == 1
            route_material = route_progress if isinstance(route_progress, dict) else {}
            try:
                topology_attempt_count = int(route_material.get("topologyCandidateAttemptCount") or 0)
            except (TypeError, ValueError):
                topology_attempt_count = 0
            route_progress_made = bool(
                route_material.get("routeProgressMadeThisTurn") is True or topology_attempt_count > 0
            )
            progress = {
                "madeProgress": bool(query_progress or candidate_progress or route_progress_made),
                "queryProgress": bool(query_progress),
                "candidateProgress": bool(candidate_progress),
                "routeProgress": bool(route_progress_made),
            }
            exact_query_frontier_has_more = bool(
                updated_slot_snapshot is not None
                and SimpleDirectionFrontierService.claim_slot_queries(updated_slot_snapshot)
            )
            if updated_slot_snapshot is None and str(record.get("schemaVersion") or "") in {
                "simple-direction-compatibility-attempt-v1",
                "simple-direction-compatibility-attempt-v2",
            }:
                exact_query_frontier_has_more = bool(
                    int(record.get("providerPage") or 0) >= 2
                    and int(record.get("providerPage") or 0) < int(record.get("maxPagesPerQuery") or 0)
                )
            route_frontier_has_more = bool(
                route_material.get("routeFeasibilityExhausted") is not True
                and int(route_material.get("unverifiedTopologyCombinationCount") or 0) > 0
            )
            if (
                strict_progress_evidence
                and normalized_disposition != "provider_pending"
                and progress["madeProgress"] is True
            ):
                normalized_frontier_status = (
                    "has_more" if exact_query_frontier_has_more or route_frontier_has_more else "poi_exhausted"
                )
            if (
                strict_progress_evidence
                and normalized_disposition != "provider_pending"
                and progress["madeProgress"] is not True
            ):
                normalized_frontier_status = "poi_exhausted"
                normalized_reason_code = "no_progress_no_query_candidate_or_route_delta"
            if str(record.get("status") or "") != "claimed":
                raise ValueError("simple_direction_compatibility_attempt_not_claimed")
            result_turn = self.db.execute(
                """SELECT 1 FROM conversation_turns
                WHERE id = ? AND session_id = ? AND role = 'assistant' AND status != 'superseded'""",
                (result_turn_identity, str(row["session_id"] or "")),
            ).fetchone()
            if result_turn is None:
                raise ValueError("simple_direction_compatibility_result_turn_missing")
            next_record = copy.deepcopy(record)
            next_record.update(
                {
                    "resultAssistantTurnId": result_turn_identity,
                    "proposalId": str(proposal_id or "") or None,
                    "proposalDelta": normalized_delta,
                    "disposition": normalized_disposition,
                    "versionDelta": 0,
                    "patchDelta": 0,
                    "routeWriteDelta": 0,
                    "updatedAt": self._now(),
                }
            )
            if normalized_disposition == "provider_pending":
                next_record["status"] = "provider_pending"
                next_record["frontierStatus"] = normalized_frontier_status
                next_record["reasonCode"] = str(normalized_reason_code or "provider_unavailable")
            else:
                if normalized_delta == 1:
                    proposal = self.db.execute(
                        "SELECT generation_lineage_json FROM agent_plan_proposals WHERE id = ? AND portfolio_id = ?",
                        (str(proposal_id), portfolio_id),
                    ).fetchone()
                    try:
                        lineage = json.loads(str(proposal["generation_lineage_json"] or "{}")) if proposal else {}
                    except (TypeError, ValueError):
                        lineage = {}
                    if (
                        str(lineage.get("workflowMode") or "") != "simple_direction_v1"
                        or str(lineage.get("frontierExecutionId") or "") != execution_identity
                        or str(lineage.get("sourceAssistantTurnId") or "") != result_turn_identity
                        or str(lineage.get("requestContractFingerprint") or "") != request_fingerprint
                        or str(lineage.get("frontierAttemptFingerprint") or "")
                        != str(record.get("attemptFingerprint") or "")
                        or int(lineage.get("itineraryWriteCount") or 0) != 0
                    ):
                        raise ValueError("simple_direction_compatibility_proposal_lineage_mismatch")
                next_record["status"] = (
                    "no_progress" if strict_progress_evidence and progress["madeProgress"] is not True else "reconciled"
                )
                next_record["frontierStatus"] = normalized_frontier_status
                next_record["progress"] = progress
                if normalized_reason_code:
                    next_record["reasonCode"] = normalized_reason_code
                else:
                    next_record.pop("reasonCode", None)
            next_record["reconcileInputFingerprint"] = reconcile_input_fingerprint
            next_record["reconcileResultFingerprint"] = self.simple_direction_compatibility_result_fingerprint(
                next_record
            )
            attempts[execution_identity] = next_record
            updated = self.db.execute(
                """UPDATE agent_plan_portfolios SET summary_json = ?, updated_at = ?
                WHERE id = ? AND summary_json = ?""",
                (self._dump(summary), self._now(), portfolio_id, raw_summary),
            )
            if updated.rowcount == 1:
                if result_response_payload is not None:
                    response_payload = copy.deepcopy(result_response_payload)
                    expected_payload_fields = {
                        "mode": "simple_open_direction_proposal",
                        "workflowMode": "simple_direction_v1",
                        "planningSelectionRootTurnId": str(row["source_user_turn_id"] or ""),
                        "rootPortfolioId": str(portfolio_id),
                        "requestContractFingerprint": request_fingerprint,
                        "frontierExecutionId": execution_identity,
                        "proposalDelta": normalized_delta,
                        "frontierStatus": normalized_frontier_status,
                        "versionDelta": 0,
                        "patchDelta": 0,
                        "routeWriteDelta": 0,
                    }
                    mismatch_fields = [
                        key
                        for key, expected in expected_payload_fields.items()
                        if (
                            int(response_payload.get(key) or 0) != expected
                            if isinstance(expected, int)
                            else str(response_payload.get(key) or "") != expected
                        )
                    ]
                    if response_payload.get("frontierAttemptConsumed") is not (
                        normalized_disposition != "provider_pending"
                    ):
                        mismatch_fields.append("frontierAttemptConsumed")
                    if mismatch_fields:
                        if "frontierStatus" in mismatch_fields:
                            mismatch_fields[mismatch_fields.index("frontierStatus")] = (
                                "frontierStatus["
                                + str(response_payload.get("frontierStatus") or "")
                                + "->"
                                + normalized_frontier_status
                                + "]"
                            )
                        self.db.rollback()
                        raise ValueError(
                            "simple_direction_compatibility_result_payload_mismatch:"
                            + ",".join(mismatch_fields)
                        )
                    persisted_result = self.db.execute(
                        """UPDATE conversation_turns SET agent_response_json = ?, updated_at = ?
                        WHERE id = ? AND session_id = ? AND role = 'assistant' AND status != 'superseded'""",
                        (
                            self._dump(response_payload),
                            self._now(),
                            result_turn_identity,
                            str(row["session_id"] or ""),
                        ),
                    )
                    if persisted_result.rowcount != 1:
                        self.db.rollback()
                        raise ValueError("simple_direction_compatibility_result_turn_missing")
                self.db.commit()
                return copy.deepcopy(next_record)
            self.db.rollback()
        raise ValueError("simple_direction_compatibility_attempt_stale")

    def fail_simple_direction_compatibility_attempt(
        self,
        *,
        portfolio_id: str,
        execution_id: str,
        reason_code: str,
        expected_execution_updated_at: str,
    ) -> dict[str, Any]:
        """Atomically terminalize an abandoned claim and its execution lease."""

        execution_identity = str(execution_id or "").strip()
        normalized_reason = str(reason_code or "compatibility_execution_lease_expired").strip()
        if not execution_identity:
            raise ValueError("simple_direction_compatibility_execution_missing")
        for _attempt in range(4):
            row = self.db.execute(
                "SELECT summary_json FROM agent_plan_portfolios WHERE id = ?",
                (portfolio_id,),
            ).fetchone()
            if row is None:
                raise ValueError("plan_portfolio_not_found")
            raw_summary = str(row["summary_json"] or "{}")
            try:
                summary = json.loads(raw_summary)
            except (TypeError, ValueError):
                summary = {}
            attempts = summary.get("simpleDirectionCompatibilityAttempts")
            record = attempts.get(execution_identity) if isinstance(attempts, dict) else None
            if not isinstance(record, dict):
                raise ValueError("simple_direction_compatibility_attempt_missing")
            if str(record.get("executionId") or "") != execution_identity or str(
                record.get("attemptFingerprint") or ""
            ) != self.simple_direction_compatibility_attempt_fingerprint(record):
                raise ValueError("simple_direction_compatibility_attempt_identity_mismatch")
            status = str(record.get("status") or "")
            if status == "failed_terminal":
                if str(record.get("reasonCode") or "") != normalized_reason:
                    raise ValueError("simple_direction_compatibility_failure_replay_mismatch")
                execution = self.db.execute(
                    "SELECT status FROM agent_choice_executions WHERE id = ?",
                    (execution_identity,),
                ).fetchone()
                if execution is not None and str(execution["status"] or "") == "failed_terminal":
                    return copy.deepcopy(record)
                raise ValueError("simple_direction_compatibility_failure_execution_mismatch")
            if status != "claimed":
                raise ValueError("simple_direction_compatibility_attempt_not_claimed")
            failed = copy.deepcopy(record)
            failed.update(
                {
                    "status": "failed_terminal",
                    "frontierStatus": "failed_terminal",
                    "reasonCode": normalized_reason,
                    "versionDelta": 0,
                    "patchDelta": 0,
                    "routeWriteDelta": 0,
                    "updatedAt": self._now(),
                }
            )
            attempts[execution_identity] = failed
            updated = self.db.execute(
                """UPDATE agent_plan_portfolios SET summary_json = ?, updated_at = ?
                WHERE id = ? AND summary_json = ?""",
                (self._dump(summary), self._now(), portfolio_id, raw_summary),
            )
            if updated.rowcount == 1:
                outcome = {
                    "passed": False,
                    "reason": normalized_reason,
                    "frontierAttemptConsumed": False,
                    "versionDelta": 0,
                    "patchDelta": 0,
                    "routeWriteDelta": 0,
                    "zeroWrite": True,
                }
                execution_updated = self.db.execute(
                    """UPDATE agent_choice_executions
                    SET status = 'failed_terminal', outcome_json = ?, error_json = ?, updated_at = ?
                    WHERE id = ? AND status = 'executing' AND updated_at = ?""",
                    (
                        self._dump(outcome),
                        self._dump({"code": normalized_reason, "retryable": False}),
                        self._now(),
                        execution_identity,
                        str(expected_execution_updated_at or ""),
                    ),
                )
                if execution_updated.rowcount != 1:
                    self.db.rollback()
                    raise ValueError("simple_direction_compatibility_failure_execution_stale")
                self.db.commit()
                return copy.deepcopy(failed)
            self.db.rollback()
        raise ValueError("simple_direction_compatibility_attempt_stale")

    @staticmethod
    def _bind_simple_direction_qualification_entities(
        *,
        attempt: dict[str, Any],
        frontier: dict[str, Any],
        summary: dict[str, Any],
        planning_root_id: str,
        request_contract_fingerprint: str,
    ) -> None:
        """Attach current evidence to an exact frozen entity/root assignment.

        Legacy frontiers are upgraded only in the newly claimed attempt.  Their
        cursor fingerprint is left untouched, so reconciliation remains
        monotonic and no historical page is consumed twice.
        """

        request_contract = (
            summary.get("requestIntentContract") if isinstance(summary.get("requestIntentContract"), dict) else {}
        )
        qualification = (
            request_contract.get("entityQualificationConstraint")
            if isinstance(request_contract.get("entityQualificationConstraint"), dict)
            else {}
        )
        scheme = str(qualification.get("qualificationScheme") or "").strip()
        value = str(qualification.get("qualificationValue") or "").strip()
        if not scheme or not value:
            # Generic frontier tests and non-qualified entity frontiers retain
            # their original strict candidate-text behavior.
            return
        assignment_localities = {
            str(assignment.get("locality") or "").strip()
            for assignment in attempt.get("campusAssignments") or []
            if isinstance(assignment, dict) and str(assignment.get("locality") or "").strip()
        }
        if len(assignment_localities) != 1:
            raise ValueError("simple_direction_qualification_binding_locality_mismatch")
        evidence = EntityQualificationEvidenceService.qualified_entities(
            locality=next(iter(assignment_localities)),
            scheme=scheme,
            value=value,
        )
        if not isinstance(evidence, dict):
            raise ValueError("simple_direction_qualification_evidence_missing")
        if str(frontier.get("qualificationEvidenceFingerprint") or "") != str(evidence.get("contentSha256") or ""):
            raise ValueError("simple_direction_qualification_evidence_epoch_mismatch")
        entities_by_fingerprint = {
            str(item.get("evidenceEntityFingerprint") or ""): item
            for item in frontier.get("qualifiedEntityFrontier") or []
            if isinstance(item, dict) and str(item.get("evidenceEntityFingerprint") or "")
        }
        for assignment in attempt.get("campusAssignments") or []:
            if not isinstance(assignment, dict):
                continue
            entity_fingerprint = str(assignment.get("evidenceEntityFingerprint") or "")
            frontier_entity = entities_by_fingerprint.get(entity_fingerprint, {})
            canonical_name = str(assignment.get("canonicalName") or "").strip()
            evidence_entity = next(
                (
                    item
                    for item in evidence.get("entities") or []
                    if isinstance(item, dict)
                    and str(item.get("canonicalName") or "").strip() == canonical_name
                    and EntityQualificationEvidenceService.entity_fingerprint(
                        evidence_fingerprint=str(evidence.get("contentSha256") or ""),
                        entity=item,
                    )
                    == entity_fingerprint
                ),
                None,
            )
            if evidence_entity is None:
                raise ValueError("simple_direction_qualification_binding_entity_mismatch")
            binding = assignment.get("qualificationBinding")
            if not isinstance(binding, dict):
                binding = frontier_entity.get("qualificationBinding")
            if not isinstance(binding, dict):
                binding = EntityQualificationEvidenceService.build_binding(
                    evidence=evidence,
                    entity=evidence_entity,
                    planning_root_id=planning_root_id,
                    request_contract_fingerprint=request_contract_fingerprint,
                )
            binding_reason = EntityQualificationEvidenceService.validate_binding(
                binding,
                expected_planning_root_id=planning_root_id,
                expected_request_contract_fingerprint=request_contract_fingerprint,
                expected_entity_fingerprint=entity_fingerprint,
                expected_canonical_name=canonical_name,
            )
            if binding_reason:
                if binding_reason == "qualification_binding_evidence_epoch_mismatch":
                    raise ValueError("simple_direction_qualification_evidence_epoch_mismatch")
                raise ValueError(f"simple_direction_{binding_reason}")
            assignment["qualificationBinding"] = copy.deepcopy(binding)
            assignment["qualificationBindingFingerprint"] = str(binding.get("bindingFingerprint") or "")

    def reconcile_simple_direction_frontier_attempt(
        self,
        *,
        portfolio_id: str,
        execution_id: str,
        outcomes: list[dict[str, Any]],
        slot_query_outcomes: Optional[list[dict[str, Any]]] = None,
        proposal_id: Optional[str],
        disposition: str,
        expected_request_contract_fingerprint: str,
        reason_code: str = "",
        blocking_layer: str = "",
        continuation_metadata: Optional[dict[str, Any]] = None,
        remaining_query_scopes: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        """Settle one claimed assignment, preserving Provider failures in place."""

        execution_identity = str(execution_id or "").strip()
        if not execution_identity:
            raise ValueError("simple_direction_frontier_execution_identity_missing")
        if not SimpleDirectionFrontierService.is_valid_blocking_layer(blocking_layer):
            raise ValueError("simple_direction_frontier_blocking_layer_invalid")
        for _attempt in range(4):
            row = self.db.execute(
                """SELECT summary_json, request_contract_fingerprint
                FROM agent_plan_portfolios WHERE id = ?""",
                (portfolio_id,),
            ).fetchone()
            if row is None:
                raise ValueError("plan_portfolio_not_found")
            request_fingerprint = str(row["request_contract_fingerprint"] or "")
            if request_fingerprint != str(expected_request_contract_fingerprint or ""):
                raise ValueError("portfolio_request_fingerprint_mismatch")
            raw_summary = str(row["summary_json"] or "{}")
            try:
                summary = json.loads(raw_summary)
            except (TypeError, ValueError):
                summary = {}
            frontier = summary.get("simpleDirectionFrontier")
            attempts = summary.get("simpleDirectionFrontierAttempts")
            if not isinstance(frontier, dict) or not isinstance(attempts, dict):
                raise ValueError("simple_direction_frontier_missing")
            record = attempts.get(execution_identity)
            if not isinstance(record, dict) or not isinstance(record.get("attempt"), dict):
                raise ValueError("simple_direction_frontier_attempt_missing")
            if str(record.get("requestContractFingerprint") or "") != request_fingerprint:
                raise ValueError("simple_direction_frontier_attempt_identity_mismatch")
            if str(record.get("status") or "") == "reconciled":
                return {
                    "frontier": copy.deepcopy(frontier),
                    "attempt": copy.deepcopy(record["attempt"]),
                    "claim": copy.deepcopy(record),
                }
            if str(record.get("status") or "") != "claimed":
                raise ValueError("simple_direction_frontier_attempt_not_claimed")
            normalized_outcomes = SimpleDirectionFrontierService.validate_attempt_outcomes(
                attempt=record["attempt"],
                outcomes=outcomes,
            )
            next_record = copy.deepcopy(record)
            next_record["outcomes"] = [copy.deepcopy(item) for item in normalized_outcomes]
            supplied_slot_outcomes = list(slot_query_outcomes or [])
            if any(not isinstance(item, dict) for item in supplied_slot_outcomes):
                raise ValueError("simple_direction_slot_frontier_outcomes_invalid")
            normalized_slot_outcomes = [copy.deepcopy(item) for item in supplied_slot_outcomes]
            next_record["slotQueryOutcomes"] = normalized_slot_outcomes
            next_frontier = copy.deepcopy(frontier)
            validated_slot_queries: list[tuple[dict[str, Any], dict[str, Any]]] = []
            campus_by_day = {
                int(assignment.get("dayNumber") or 0): str(outcome.get("selectedAmapId") or "").strip().upper()
                for assignment in record["attempt"].get("campusAssignments") or []
                if isinstance(assignment, dict)
                for outcome in normalized_outcomes
                if isinstance(outcome, dict)
                and str(outcome.get("slotId") or "") == str(assignment.get("slotId") or "")
                and str(outcome.get("selectedAmapId") or "").strip()
            }
            for slot_outcome in normalized_slot_outcomes:
                query = slot_outcome.get("query") if isinstance(slot_outcome.get("query"), dict) else {}
                day_number = int(query.get("dayNumber") or 0)
                day_seed_amap_id = str(query.get("daySeedAmapId") or "").strip().upper()
                if not day_number or not day_seed_amap_id or campus_by_day.get(day_number) != day_seed_amap_id:
                    raise ValueError("simple_direction_slot_frontier_day_seed_mismatch")
                expected_query = SimpleDirectionFrontierService.begin_slot_query(
                    next_frontier,
                    day_number=day_number,
                    slot_id=str(query.get("slotId") or ""),
                    day_seed_amap_id=day_seed_amap_id,
                    query_scope_fingerprint=str(query.get("queryScopeFingerprint") or ""),
                )
                for identity_field in (
                    "slotFrontierKey",
                    "queryFingerprint",
                    "page",
                    "offset",
                    "requestContractFingerprint",
                ):
                    if str(query.get(identity_field) or "") != str(expected_query.get(identity_field) or ""):
                        raise ValueError("simple_direction_slot_frontier_query_identity_mismatch")
                if expected_query.get("exhausted") is True:
                    raise ValueError("simple_direction_slot_frontier_query_exhausted")
                validated_slot_queries.append((copy.deepcopy(query), slot_outcome))
            if disposition == "provider_pending":
                # Transport failure is not evidence that the entity or page was
                # exhausted.  Release the active claim while retaining the same
                # assignment for an explicit retry.
                next_record["status"] = "provider_pending"
                next_record["reasonCode"] = str(reason_code or "provider_unavailable")
                next_record["blockingLayer"] = "provider"
                next_frontier["terminalBlockingLayer"] = "provider"
                next_frontier["terminalReasonCode"] = next_record["reasonCode"]
                next_frontier["providerRecoveryPending"] = True
                SimpleDirectionFrontierService._refresh_status(next_frontier)
            else:
                next_frontier.pop("providerRecoveryPending", None)
                next_frontier = SimpleDirectionFrontierService.reconcile_attempt(
                    next_frontier,
                    attempt=record["attempt"],
                    outcomes=normalized_outcomes,
                    proposal_id=proposal_id,
                    disposition=disposition,
                    blocking_layer=blocking_layer,
                    reason_code=reason_code,
                    continuation_metadata=continuation_metadata,
                    remaining_query_scopes=remaining_query_scopes,
                )
                next_record["status"] = "reconciled"
                next_record["disposition"] = disposition
                next_record["proposalId"] = str(proposal_id or "") or None
                next_record["resultFrontierFingerprint"] = str(next_frontier.get("frontierFingerprint") or "")
                if str(reason_code or ""):
                    next_record["reasonCode"] = str(reason_code)
                else:
                    next_record.pop("reasonCode", None)
                if str(blocking_layer or ""):
                    next_record["blockingLayer"] = str(blocking_layer)
                else:
                    next_record.pop("blockingLayer", None)
            for query, slot_outcome in validated_slot_queries:
                next_frontier = SimpleDirectionFrontierService.record_slot_query(
                    next_frontier,
                    query=query,
                    provider_outcome=str(slot_outcome.get("providerOutcome") or ""),
                    admitted_physical_groups=[
                        str(item) for item in slot_outcome.get("admittedPhysicalGroups") or [] if str(item)
                    ],
                    rejected_physical_groups=[
                        copy.deepcopy(item)
                        for item in slot_outcome.get("rejectedPhysicalGroups") or []
                        if isinstance(item, dict)
                    ],
                )
            next_record["resultFrontierFingerprint"] = str(next_frontier.get("frontierFingerprint") or "")
            next_record["updatedAt"] = self._now()
            attempts[execution_identity] = next_record
            summary["simpleDirectionFrontier"] = next_frontier
            updated = self.db.execute(
                """UPDATE agent_plan_portfolios SET summary_json = ?, updated_at = ?
                WHERE id = ? AND summary_json = ?""",
                (self._dump(summary), self._now(), portfolio_id, raw_summary),
            )
            if updated.rowcount == 1:
                self.db.commit()
                return {
                    "frontier": copy.deepcopy(next_frontier),
                    "attempt": copy.deepcopy(record["attempt"]),
                    "claim": copy.deepcopy(next_record),
                }
            self.db.rollback()
        raise ValueError("simple_direction_frontier_stale")

    def sync_expected_base_version(self, *, portfolio_id: str, active_version_id: str) -> None:
        """Keep the root row and its summary identity on the same active base."""

        row = self.db.execute(
            "SELECT summary_json FROM agent_plan_portfolios WHERE id = ?",
            (portfolio_id,),
        ).fetchone()
        if row is None:
            raise ValueError("plan_portfolio_not_found")
        try:
            summary = json.loads(str(row["summary_json"] or "{}"))
        except (TypeError, ValueError):
            summary = {}
        summary["expectedBaseVersionId"] = str(active_version_id or "")
        self.db.execute(
            """UPDATE agent_plan_portfolios
            SET expected_base_version_id = ?, summary_json = ?, updated_at = ?
            WHERE id = ?""",
            (str(active_version_id or ""), self._dump(summary), self._now(), portfolio_id),
        )
        self.db.commit()

    def classification_counts(self, *, portfolio_id: str) -> dict[str, int]:
        counts = self._classification_counts(portfolio_id)
        self._persist_classification_counts(portfolio_id, counts=counts)
        self.db.commit()
        return counts

    def simple_direction_comparison_summary(self, *, portfolio_id: str, persist: bool = True) -> dict[str, Any]:
        """Persist and return the UI-facing counts from root/proposal truth."""

        counts = self._classification_counts(portfolio_id)
        for _attempt in range(4):
            row = self.db.execute(
                "SELECT summary_json FROM agent_plan_portfolios WHERE id = ?",
                (portfolio_id,),
            ).fetchone()
            if row is None:
                raise ValueError("plan_portfolio_not_found")
            raw_summary = str(row["summary_json"] or "{}")
            try:
                summary = json.loads(raw_summary)
            except (TypeError, ValueError):
                summary = {}
            frontier = summary.get("simpleDirectionFrontier")
            frontier = frontier if isinstance(frontier, dict) else {}
            if frontier:
                # Lazily normalize roots persisted before proposal-scoped
                # route Provider blockers were separated from the remaining
                # qualification-entity frontier.  This does not call a
                # Provider or advance a cursor; it only recomputes status from
                # the already persisted entity/slot state.
                SimpleDirectionFrontierService._refresh_status(frontier)
                summary["simpleDirectionFrontier"] = frontier
            attempts = summary.get("simpleDirectionFrontierAttempts")
            attempts = attempts if isinstance(attempts, dict) else {}
            compatibility_attempts = summary.get("simpleDirectionCompatibilityAttempts")
            compatibility_attempts = compatibility_attempts if isinstance(compatibility_attempts, dict) else {}
            entities = [item for item in frontier.get("qualifiedEntityFrontier") or [] if isinstance(item, dict)]
            provider_pending = any(
                isinstance(item, dict) and str(item.get("status") or "") == "provider_pending"
                for item in attempts.values()
            )
            latest_attempt = max(
                (item for item in attempts.values() if isinstance(item, dict)),
                key=lambda item: str(item.get("updatedAt") or item.get("claimedAt") or ""),
                default={},
            )
            latest_compatibility_attempt = max(
                (item for item in compatibility_attempts.values() if isinstance(item, dict)),
                key=lambda item: str(item.get("updatedAt") or item.get("claimedAt") or ""),
                default={},
            )
            compatibility_projection = self._simple_direction_compatibility_frontier_projection(
                portfolio_id=portfolio_id,
                counts=counts,
            )
            compatibility_provider_pending = any(
                isinstance(item, dict) and str(item.get("status") or "") == "provider_pending"
                for item in compatibility_attempts.values()
            )
            compatibility_pages = {
                int(str(item.get("providerPage") or "0"))
                for item in compatibility_attempts.values()
                if isinstance(item, dict)
                and str(item.get("providerPage") or "0").isdigit()
                and int(str(item.get("providerPage") or "0")) >= 2
            }
            attempted_page_count = (
                sum(
                    len({int(page) for page in item.get("attemptedPages") or [] if str(page).isdigit()})
                    for item in entities
                )
                + sum(
                    len({int(page) for page in slot_frontier.get("attemptedPages") or [] if str(page).isdigit()})
                    for slot_frontier in (frontier.get("slotFrontiers") or {}).values()
                    if isinstance(slot_frontier, dict)
                )
                + len(compatibility_pages)
            )
            frontier_status = (
                "provider_pending"
                if provider_pending or compatibility_provider_pending
                else str(frontier.get("frontierStatus") or "")
                if frontier
                else str(
                    compatibility_projection.get("frontierStatus")
                    or latest_compatibility_attempt.get("frontierStatus")
                    or "qualification_exhausted"
                )
            )
            blocking_layer = {
                "provider_pending": "provider",
                "qualification_exhausted": "qualification",
                "poi_exhausted": "poi",
                "route_feasible_exhausted": "route",
            }.get(frontier_status)
            compatibility_frontier = summary.get("simpleDirectionCompatibilityFrontier")
            compatibility_frontier = compatibility_frontier if isinstance(compatibility_frontier, dict) else {}
            if compatibility_projection.get("remainingPoiPageCount") is not None:
                compatibility_remaining_pages = max(
                    0,
                    int(compatibility_projection.get("remainingPoiPageCount") or 0),
                )
            elif compatibility_frontier and frontier_status == "has_more":
                compatibility_remaining_pages = max(
                    max(1, int(compatibility_frontier.get("maxPagesPerQuery") or 1)) - max({1, *compatibility_pages}),
                    0,
                )
            elif compatibility_pages and frontier_status == "has_more":
                compatibility_remaining_pages = max(
                    int(latest_compatibility_attempt.get("maxPagesPerQuery") or 0) - max(compatibility_pages),
                    0,
                )
            else:
                compatibility_remaining_pages = 0
            material = {
                "adoptionReadyCount": int(counts.get("adoptionReadyProposalCount") or 0),
                "repairablePartialCount": int(counts.get("partialComparisonProposalCount") or 0),
                "remainingQualifiedEntityCount": int(frontier.get("remainingQualifiedEntityCount") or 0),
                "remainingPoiPageCount": (
                    int(frontier.get("remainingPoiPageCount") or 0) if frontier else compatibility_remaining_pages
                ),
                "frontierStatus": frontier_status,
                "exploredQualifiedEntityCount": sum(
                    str(item.get("state") or "") not in {"", "untried", "grounding_pending"} for item in entities
                ),
                "attemptedPoiPageCount": attempted_page_count,
                "lastOutcomeReason": str(
                    latest_attempt.get("reasonCode")
                    or frontier.get("terminalReasonCode")
                    or latest_attempt.get("disposition")
                    or latest_compatibility_attempt.get("reasonCode")
                    or latest_compatibility_attempt.get("disposition")
                    or compatibility_projection.get("reasonCode")
                    or ""
                )
                or None,
                "blockingLayer": (
                    "provider"
                    if provider_pending or compatibility_provider_pending
                    else str(latest_attempt.get("blockingLayer") or "")
                    or str(frontier.get("terminalBlockingLayer") or "")
                    or str(compatibility_projection.get("blockingLayer") or "")
                    or blocking_layer
                ),
            }
            summary.update(counts)
            summary["comparisonSummary"] = material
            if not persist:
                return material
            updated = self.db.execute(
                """UPDATE agent_plan_portfolios SET summary_json = ?, updated_at = ?
                WHERE id = ? AND summary_json = ?""",
                (self._dump(summary), self._now(), portfolio_id, raw_summary),
            )
            if updated.rowcount == 1:
                self.db.commit()
                return material
            self.db.rollback()
        raise ValueError("simple_direction_frontier_stale")

    def _simple_direction_compatibility_frontier_projection(
        self,
        *,
        portfolio_id: str,
        counts: dict[str, int],
    ) -> dict[str, Any]:
        """Project only evidence-backed expansion state for roots without a frontier."""

        root = self.db.execute(
            "SELECT summary_json FROM agent_plan_portfolios WHERE id = ?",
            (portfolio_id,),
        ).fetchone()
        try:
            summary = json.loads(str(root["summary_json"] or "{}")) if root is not None else {}
        except (TypeError, ValueError):
            summary = {}
        summary = summary if isinstance(summary, dict) else {}
        compatibility_frontier = summary.get("simpleDirectionCompatibilityFrontier")
        if isinstance(compatibility_frontier, dict):
            slot_snapshot = (
                compatibility_frontier.get("slotFrontierSnapshot")
                if isinstance(compatibility_frontier.get("slotFrontierSnapshot"), dict)
                else {}
            )
            if slot_snapshot.get("remainingQueryScopesAuthoritative") is True:
                exact_snapshot = copy.deepcopy(slot_snapshot)
                SimpleDirectionFrontierService._refresh_status(exact_snapshot)
                exact_status = str(exact_snapshot.get("frontierStatus") or "poi_exhausted")
                exact_remaining_pages = int(exact_snapshot.get("remainingPoiPageCount") or 0)
                return {
                    "frontierStatus": exact_status,
                    "remainingPoiPageCount": exact_remaining_pages,
                    "blockingLayer": (
                        "provider"
                        if exact_status == "provider_pending"
                        else "poi"
                        if exact_status == "poi_exhausted"
                        else None
                    ),
                    "reasonCode": (
                        "compatibility_provider_pending"
                        if exact_status == "provider_pending"
                        else "compatibility_exact_scopes_exhausted"
                        if exact_status == "poi_exhausted"
                        else None
                    ),
                }
            max_pages = max(1, int(compatibility_frontier.get("maxPagesPerQuery") or 1))
            attempts = summary.get("simpleDirectionCompatibilityAttempts")
            attempts = attempts if isinstance(attempts, dict) else {}
            attempted_pages = {
                int(str(item.get("providerPage") or "0"))
                for item in attempts.values()
                if isinstance(item, dict)
                and str(item.get("providerPage") or "0").isdigit()
                and int(str(item.get("providerPage") or "0")) >= 2
            }
            if max({1, *attempted_pages}) >= max_pages:
                return {
                    "frontierStatus": "poi_exhausted",
                    "blockingLayer": "poi",
                    "reasonCode": "compatibility_page_limit_reached",
                }
        if int(counts.get("adoptionReadyProposalCount") or 0) > 0:
            return {"frontierStatus": "has_more", "blockingLayer": None, "reasonCode": None}
        visible_ids = self._visible_proposal_ids(root["summary_json"]) if root is not None else []
        if not visible_ids:
            return {
                "frontierStatus": "qualification_exhausted",
                "blockingLayer": "qualification",
                "reasonCode": "no_visible_direction_material",
            }
        placeholders = ", ".join("?" for _ in visible_ids)
        rows = self.db.execute(
            f"""SELECT id, snapshot_json FROM agent_plan_proposals
            WHERE portfolio_id = ? AND id IN ({placeholders})""",
            (portfolio_id, *visible_ids),
        ).fetchall()
        route_failure_reason = ""
        provider_exhausted_reason = ""
        for row in rows:
            try:
                snapshot = json.loads(str(row["snapshot_json"] or "{}"))
            except (TypeError, ValueError):
                snapshot = {}
            snapshot = snapshot if isinstance(snapshot, dict) else {}
            pending_slots = [item for item in snapshot.get("portfolioPendingSlots") or [] if isinstance(item, dict)]
            if any(
                item.get("simpleDirectionProviderExhausted") is True
                and item.get("simpleDirectionRequirementLineageConflict") is not True
                and str(item.get("requirementLevel") or "required") in {"hard", "required"}
                for item in pending_slots
            ):
                # This is the only no-frontier partial that response_material
                # is allowed to pair with one server-signed continuation.
                return {"frontierStatus": "has_more", "blockingLayer": None, "reasonCode": None}
            route_audit = (
                snapshot.get("simpleOpenRouteAssignment")
                if isinstance(snapshot.get("simpleOpenRouteAssignment"), dict)
                else {}
            )
            route_failure_reason = route_failure_reason or str(route_audit.get("failureReason") or "")
            exhausted_slot = next(
                (
                    item
                    for item in pending_slots
                    if item.get("simpleDirectionProviderExhausted") is True
                    and item.get("simpleDirectionRequirementLineageConflict") is not True
                ),
                None,
            )
            if isinstance(exhausted_slot, dict):
                provider_exhausted_reason = provider_exhausted_reason or str(
                    exhausted_slot.get("reasonCode")
                    or exhausted_slot.get("sourceReasonCode")
                    or "provider_candidates_exhausted_or_semantically_rejected"
                )
        if route_failure_reason:
            return {
                "frontierStatus": "route_feasible_exhausted",
                "blockingLayer": "route",
                "reasonCode": route_failure_reason,
            }
        if provider_exhausted_reason:
            return {
                "frontierStatus": "poi_exhausted",
                "blockingLayer": "poi",
                "reasonCode": provider_exhausted_reason,
            }
        return {
            "frontierStatus": "qualification_exhausted",
            "blockingLayer": "qualification",
            "reasonCode": "no_server_continuation_capability",
        }

    def _classification_counts(self, portfolio_id: str) -> dict[str, int]:
        root = self.db.execute(
            "SELECT summary_json FROM agent_plan_portfolios WHERE id = ?",
            (portfolio_id,),
        ).fetchone()
        visible_ids = self._visible_proposal_ids(root["summary_json"]) if root is not None else []
        counts = {
            "visibleComparisonProposalCount": len(visible_ids),
            "partialComparisonProposalCount": 0,
            "verifiedComparisonProposalCount": 0,
            "adoptionReadyProposalCount": 0,
        }
        if not visible_ids:
            return counts
        placeholders = ", ".join("?" for _ in visible_ids)
        rows = self.db.execute(
            f"""SELECT id, status, snapshot_json, verifier_json
            FROM agent_plan_proposals
            WHERE portfolio_id = ? AND id IN ({placeholders})""",
            (portfolio_id, *visible_ids),
        ).fetchall()
        material_by_id = {str(row["id"]): row for row in rows}
        for proposal_id in visible_ids:
            row = material_by_id.get(proposal_id)
            if row is None:
                counts["partialComparisonProposalCount"] += 1
                continue
            try:
                snapshot = json.loads(str(row["snapshot_json"] or "{}"))
                verifier = json.loads(str(row["verifier_json"] or "{}"))
            except (TypeError, ValueError):
                counts["partialComparisonProposalCount"] += 1
                continue
            verifier = verifier if isinstance(verifier, dict) else {}
            compact_route_contract = verifier.get("compactRouteContractRequired") is True
            if compact_route_contract:
                # Simple Open already freezes and validates the four final
                # Provider route pairs in ``confirmationPassed``.  Re-running
                # the generic portfolio readiness calculator here applies a
                # different evidence shape and can turn an adoption-ready card
                # into a partial after its confirmation choice was issued.
                lifecycle_ready = str(row["status"] or "") in {"adoption_ready", "committed"}
                authoritative_ready = verifier.get("confirmationPassed") is True and lifecycle_ready
                readiness = {
                    "strictlyVerified": authoritative_ready,
                    "adoptionReady": authoritative_ready,
                }
            else:
                readiness = ProposalReadinessService.compute(
                    snapshot if isinstance(snapshot, dict) else {},
                    verifier=verifier,
                    # Incomplete Provider route evidence must never increase
                    # the server-authoritative adoption-ready count.
                    route_failures_non_blocking=False,
                )
            if readiness.get("strictlyVerified") is True:
                counts["verifiedComparisonProposalCount"] += 1
            else:
                counts["partialComparisonProposalCount"] += 1
            if readiness.get("adoptionReady") is True:
                counts["adoptionReadyProposalCount"] += 1
        return counts

    def _persist_classification_counts(
        self,
        portfolio_id: str,
        *,
        counts: dict[str, int] | None = None,
    ) -> None:
        row = self.db.execute(
            "SELECT summary_json FROM agent_plan_portfolios WHERE id = ?",
            (portfolio_id,),
        ).fetchone()
        if row is None:
            return
        try:
            summary = json.loads(str(row["summary_json"] or "{}"))
        except (TypeError, ValueError):
            summary = {}
        summary.update(counts or self._classification_counts(portfolio_id))
        self.db.execute(
            "UPDATE agent_plan_portfolios SET summary_json = ?, updated_at = ? WHERE id = ?",
            (self._dump(summary), self._now(), portfolio_id),
        )

    def load_choice(
        self, *, session_id: str, source_user_turn_id: str, choice_id: str
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        row = self.db.execute(
            """SELECT
                p.id AS portfolio_id, p.session_id, p.source_user_turn_id, p.source_assistant_turn_id,
                p.expected_base_version_id, p.source_observation_fingerprint, p.request_contract_fingerprint,
                p.status AS portfolio_status, p.selected_proposal_id, p.dominant_proposal_id, p.failure_reason,
                p.expires_at, p.summary_json,
                p.updated_at AS portfolio_updated_at,
                q.id AS proposal_id, q.portfolio_id AS proposal_portfolio_id, q.choice_id,
                q.rank_index, q.status AS proposal_status, q.brief_json, q.snapshot_json, q.score_json,
                q.verifier_json, q.evidence_json, q.canonical_signature, q.generation_lineage_json
            FROM agent_plan_portfolios p
            JOIN agent_plan_proposals q ON q.portfolio_id = p.id
            WHERE p.session_id = ? AND p.source_user_turn_id = ? AND q.choice_id = ?""",
            (session_id, source_user_turn_id, choice_id),
        ).fetchone()
        if row is None:
            return None
        raw = dict(row)
        try:
            portfolio_summary = json.loads(str(raw.get("summary_json") or "{}"))
        except (TypeError, ValueError):
            portfolio_summary = {}
        if not isinstance(portfolio_summary, dict):
            portfolio_summary = {}
        portfolio = {
            "id": raw["portfolio_id"],
            "session_id": raw["session_id"],
            "source_user_turn_id": raw["source_user_turn_id"],
            "source_assistant_turn_id": raw["source_assistant_turn_id"],
            "expected_base_version_id": raw["expected_base_version_id"],
            "source_observation_fingerprint": raw["source_observation_fingerprint"],
            "request_contract_fingerprint": raw["request_contract_fingerprint"],
            "status": raw["portfolio_status"],
            "selected_proposal_id": raw["selected_proposal_id"],
            "dominant_proposal_id": raw["dominant_proposal_id"],
            "failure_reason": raw["failure_reason"],
            "expires_at": raw["expires_at"],
            "updated_at": raw["portfolio_updated_at"],
            "visible_proposal_ids": self._visible_proposal_ids(raw["summary_json"]),
            "summary": portfolio_summary,
            "workflow_mode": str(portfolio_summary.get("workflowMode") or ""),
        }
        proposal = {
            "id": raw["proposal_id"],
            "portfolio_id": raw["proposal_portfolio_id"],
            "choice_id": raw["choice_id"],
            "rank_index": raw["rank_index"],
            "status": raw["proposal_status"],
            "brief_json": raw["brief_json"],
            "snapshot_json": raw["snapshot_json"],
            "score_json": raw["score_json"],
            "verifier_json": raw["verifier_json"],
            "evidence_json": raw["evidence_json"],
            "canonical_signature": raw["canonical_signature"],
            "generation_lineage_json": raw["generation_lineage_json"],
        }
        if portfolio["status"] == "awaiting_selection" and self._is_expired(portfolio.get("expires_at")):
            self._expire_portfolio(str(portfolio["id"]), "plan_portfolio_expired")
            portfolio["status"] = "expired"
            portfolio["failure_reason"] = "plan_portfolio_expired"
            proposal["status"] = "expired"
        return portfolio, proposal

    @staticmethod
    def _visible_proposal_ids(summary_json: Any) -> list[str]:
        try:
            summary = json.loads(str(summary_json or "{}"))
        except (TypeError, ValueError):
            return []
        return [str(item) for item in summary.get("visibleProposalIds") or [] if str(item)]

    def visible_comparison_projections(
        self,
        *,
        portfolio_id: str,
    ) -> list[dict[str, Any]]:
        """Load the immutable snapshots currently advertised by one root."""

        portfolio = self.db.execute(
            "SELECT summary_json FROM agent_plan_portfolios WHERE id = ?",
            (portfolio_id,),
        ).fetchone()
        if portfolio is None:
            return []
        visible_ids = self._visible_proposal_ids(portfolio["summary_json"])
        if not visible_ids:
            return []
        placeholders = ", ".join("?" for _ in visible_ids)
        rows = self.db.execute(
            f"""SELECT id, snapshot_json, score_json, verifier_json
            FROM agent_plan_proposals
            WHERE portfolio_id = ? AND status IN (
                'offered', 'committed', 'comparison_only', 'partial_preview', 'route_pending',
                'route_partial', 'route_ready', 'route_provider_failed',
                'adoption_ready', 'blocked'
            )
              AND id IN ({placeholders})""",
            (portfolio_id, *visible_ids),
        ).fetchall()
        snapshots_by_id: dict[str, dict[str, Any]] = {}
        for row in rows:
            try:
                snapshot = json.loads(str(row["snapshot_json"] or "{}"))
                score = json.loads(str(row["score_json"] or "{}"))
                verifier = json.loads(str(row["verifier_json"] or "{}"))
            except (TypeError, ValueError):
                continue
            if isinstance(snapshot, dict) and isinstance(score, dict) and isinstance(verifier, dict):
                snapshots_by_id[str(row["id"])] = snapshot
        return [
            {
                "proposalId": proposal_id,
                "title": (
                    CreativeProposalTitleService.sealed_server_fallback_title(snapshots_by_id[proposal_id])
                    or str(snapshots_by_id[proposal_id].get("title") or "")
                ),
                "days": snapshots_by_id[proposal_id].get("days") or [],
            }
            for proposal_id in visible_ids
            if proposal_id in snapshots_by_id
        ]

    def load_visible_proposal_snapshot(
        self,
        *,
        portfolio_id: str,
        proposal_id: str,
    ) -> dict[str, Any] | None:
        """Return one persisted visible snapshot without changing its identity."""

        portfolio = self.db.execute(
            "SELECT summary_json FROM agent_plan_portfolios WHERE id = ?",
            (portfolio_id,),
        ).fetchone()
        if portfolio is None or proposal_id not in self._visible_proposal_ids(portfolio["summary_json"]):
            return None
        row = self.db.execute(
            """SELECT brief_json, snapshot_json
            FROM agent_plan_proposals
            WHERE id = ? AND portfolio_id = ? AND status IN (
                'offered', 'committed', 'comparison_only', 'partial_preview', 'route_pending',
                'route_partial', 'route_ready', 'route_provider_failed',
                'adoption_ready', 'blocked'
            )""",
            (proposal_id, portfolio_id),
        ).fetchone()
        if row is None:
            return None
        try:
            brief = json.loads(str(row["brief_json"] or "{}"))
            snapshot = json.loads(str(row["snapshot_json"] or "{}"))
        except (TypeError, ValueError):
            return None
        if not isinstance(snapshot, dict):
            return None
        if isinstance(brief, dict) and brief:
            snapshot_brief = snapshot.get("creativeBrief")
            stored_brief_id = str(brief.get("briefId") or brief.get("brief_id") or "").strip()
            snapshot_brief_id = (
                str(snapshot_brief.get("briefId") or snapshot_brief.get("brief_id") or "").strip()
                if isinstance(snapshot_brief, dict)
                else ""
            )
            if stored_brief_id and snapshot_brief_id and stored_brief_id != snapshot_brief_id:
                raise ValueError("partial_preview_brief_identity_conflict")
            if not isinstance(snapshot_brief, dict) or not snapshot_brief:
                snapshot["creativeBrief"] = copy.deepcopy(brief)
        return copy.deepcopy(snapshot)

    def upsert_partial_preview(
        self,
        *,
        portfolio_id: str,
        proposal_id: str,
        choice_id: str,
        snapshot: dict[str, Any],
        brief: Any,
        verifier: dict[str, Any],
        score: Any,
        generation_lineage: dict[str, Any],
        status: str,
        evidence: dict[str, Any] | None = None,
    ) -> bool:
        """Persist a visible partial card in the existing proposal store.

        This is proposal material only.  It never creates an itinerary version,
        patch, route row, or candidate claim.  The same row is later refreshed
        by the route finalizer and, only after strict verification, handed to
        ``PlanProposalCommitService``.
        """

        allowed_statuses = {
            "partial_preview",
            "route_pending",
            "route_partial",
            "route_ready",
            "route_provider_failed",
            "adoption_ready",
            "blocked",
        }
        snapshot = self._truthful_snapshot_title(snapshot, verifier)
        if status not in allowed_statuses:
            raise ValueError("partial_preview_status_invalid")
        root = self.db.execute(
            "SELECT summary_json, status, expires_at FROM agent_plan_portfolios WHERE id = ?",
            (portfolio_id,),
        ).fetchone()
        if root is None:
            raise ValueError("plan_portfolio_not_found")
        if self._is_expired(root["expires_at"]):
            self._expire_portfolio(portfolio_id, "plan_portfolio_expired")
            raise ValueError("plan_portfolio_expired")
        try:
            summary = json.loads(str(root["summary_json"] or "{}"))
        except (TypeError, ValueError):
            summary = {}
        proposal_ids = [str(item) for item in summary.get("proposalIds") or [] if str(item)]
        visible_ids = [str(item) for item in summary.get("visibleProposalIds") or [] if str(item)]
        now = self._now()
        existing = self.db.execute(
            "SELECT id, brief_json FROM agent_plan_proposals WHERE id = ? AND portfolio_id = ?",
            (proposal_id, portfolio_id),
        ).fetchone()
        payload_brief = self._model_payload(brief)
        if existing is not None:
            try:
                existing_brief = json.loads(str(existing["brief_json"] or "{}"))
            except (TypeError, ValueError):
                existing_brief = {}
            existing_brief_id = (
                str(existing_brief.get("briefId") or existing_brief.get("brief_id") or "").strip()
                if isinstance(existing_brief, dict)
                else ""
            )
            incoming_brief_id = (
                str(payload_brief.get("briefId") or payload_brief.get("brief_id") or "").strip()
                if isinstance(payload_brief, dict)
                else ""
            )
            if existing_brief_id and existing_brief_id != incoming_brief_id:
                raise ValueError("partial_preview_brief_identity_conflict")
        payload_score = self._model_payload(score)
        if not isinstance(payload_score, dict):
            payload_score = {}
        payload_score.setdefault(
            "hardConstraintPassed",
            bool(verifier.get("requiredGoalCoverage") is not None and not verifier.get("hardFailures")),
        )
        if existing is None:
            rank = int(
                self.db.execute(
                    "SELECT COUNT(*) FROM agent_plan_proposals WHERE portfolio_id = ?",
                    (portfolio_id,),
                ).fetchone()[0]
            )
            if rank >= self.max_generated_proposals_per_root:
                raise ValueError("portfolio_proposal_limit_exceeded")
            canonical = "partial:" + str(proposal_id)
            self.db.execute(
                """INSERT INTO agent_plan_proposals (
                    id, portfolio_id, choice_id, rank_index, status, brief_json,
                    snapshot_json, score_json, verifier_json, evidence_json,
                    canonical_signature, generation_lineage_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    proposal_id,
                    portfolio_id,
                    choice_id,
                    rank,
                    status,
                    self._dump(payload_brief),
                    self._dump(snapshot),
                    self._dump(payload_score),
                    self._dump(verifier),
                    self._dump(evidence or {}),
                    canonical,
                    self._dump(generation_lineage),
                    now,
                    now,
                ),
            )
            created = True
        else:
            self.db.execute(
                """UPDATE agent_plan_proposals
                SET choice_id = ?, status = ?, brief_json = ?, snapshot_json = ?,
                    score_json = ?, verifier_json = ?, evidence_json = ?,
                    generation_lineage_json = ?, updated_at = ?
                WHERE id = ? AND portfolio_id = ?""",
                (
                    choice_id,
                    status,
                    self._dump(payload_brief),
                    self._dump(snapshot),
                    self._dump(payload_score),
                    self._dump(verifier),
                    self._dump(evidence or {}),
                    self._dump(generation_lineage),
                    now,
                    proposal_id,
                    portfolio_id,
                ),
            )
            created = False
        if proposal_id not in proposal_ids:
            proposal_ids.append(proposal_id)
        if proposal_id not in visible_ids and len(visible_ids) < self.max_visible_comparison_cards:
            visible_ids.append(proposal_id)
        summary.update(
            {
                "proposalIds": proposal_ids,
                "visibleProposalIds": visible_ids,
                "partialPreviewMaterial": True,
            }
        )
        focus_brief_id = (
            str(payload_brief.get("briefId") or payload_brief.get("brief_id") or "")
            if isinstance(payload_brief, dict)
            else ""
        )
        if focus_brief_id and created:
            summary["focusBriefId"] = focus_brief_id
            frontier = summary.get("creativeExplorationFrontier")
            if isinstance(frontier, dict):
                frontier = copy.deepcopy(frontier)
                frontier["currentFocusBriefId"] = focus_brief_id
                summary["creativeExplorationFrontier"] = frontier
        self.db.execute(
            """UPDATE agent_plan_portfolios
            SET status = 'awaiting_selection', failure_reason = NULL, summary_json = ?, updated_at = ?
            WHERE id = ? AND status IN ('building', 'awaiting_selection', 'failed')""",
            (self._dump(summary), now, portfolio_id),
        )
        self._persist_classification_counts(portfolio_id)
        self.db.commit()
        return created

    def update_proposal_material(
        self,
        *,
        portfolio_id: str,
        proposal_id: str,
        snapshot: dict[str, Any],
        verifier: dict[str, Any],
        status: str,
        evidence: dict[str, Any] | None = None,
        commit: bool = True,
    ) -> None:
        """Refresh route/readiness evidence without altering itinerary state."""
        snapshot = self._truthful_snapshot_title(snapshot, verifier)
        existing = self.db.execute(
            "SELECT evidence_json FROM agent_plan_proposals WHERE id = ? AND portfolio_id = ?",
            (proposal_id, portfolio_id),
        ).fetchone()
        try:
            existing_evidence = json.loads(str(existing["evidence_json"] or "{}")) if existing is not None else {}
        except (TypeError, ValueError):
            existing_evidence = {}
        if not isinstance(existing_evidence, dict):
            existing_evidence = {}
        merged_evidence = {
            **existing_evidence,
            **(copy.deepcopy(evidence) if isinstance(evidence, dict) else {}),
        }
        updated = self.db.execute(
            """UPDATE agent_plan_proposals
            SET snapshot_json = ?, verifier_json = ?, evidence_json = ?, status = ?,
                canonical_signature = ?, updated_at = ?
            WHERE id = ? AND portfolio_id = ?""",
            (
                self._dump(snapshot),
                self._dump(verifier),
                self._dump(merged_evidence),
                status,
                proposal_canonical_signature(snapshot),
                self._now(),
                proposal_id,
                portfolio_id,
            ),
        )
        if updated.rowcount != 1:
            raise ValueError("plan_proposal_material_not_found")
        self._persist_classification_counts(portfolio_id)
        if commit:
            self.db.commit()

    def offer_repaired_proposal(self, *, portfolio_id: str, proposal: PlanCandidate) -> tuple[str, str, bool]:
        """Append one readiness-verified repair without changing itinerary state."""
        if proposal.portfolio_id != portfolio_id:
            raise ValueError("proposal_portfolio_identity_mismatch")
        truthful_snapshot = self._truthful_snapshot_title(proposal.itinerary_snapshot, proposal.verifier)
        readiness = ProposalReadinessService.compute(
            truthful_snapshot,
            verifier=proposal.verifier,
        )
        if proposal.verifier.get("passed") is not True and not (
            readiness.get("adoptionReady") is True
            and readiness.get("adoptionMode") in {"editable_draft", "editable_partial"}
        ):
            raise ValueError("repaired_proposal_verifier_failed")
        row = self.db.execute(
            """SELECT status, summary_json, expires_at, source_user_turn_id,
                request_contract_fingerprint
            FROM agent_plan_portfolios WHERE id = ?""",
            (portfolio_id,),
        ).fetchone()
        if row is None or str(row["status"] or "") not in {
            "awaiting_selection",
            "failed",
        }:
            raise ValueError("portfolio_not_repairable")
        if self._is_expired(row["expires_at"]):
            self._expire_portfolio(portfolio_id, "plan_portfolio_expired")
            raise ValueError("plan_portfolio_expired")
        existing = self.db.execute(
            """SELECT id, choice_id FROM agent_plan_proposals
            WHERE portfolio_id = ? AND canonical_signature = ?""",
            (portfolio_id, proposal.canonical_signature),
        ).fetchone()
        if existing is not None:
            return str(existing["choice_id"]), str(existing["id"]), False
        count = int(
            self.db.execute(
                "SELECT COUNT(*) FROM agent_plan_proposals WHERE portfolio_id = ?",
                (portfolio_id,),
            ).fetchone()[0]
        )
        if count >= self.max_generated_proposals_per_root:
            raise ValueError("portfolio_proposal_limit_exceeded")
        now = self._now()
        choice_id = self._choice_id(proposal)
        self.db.execute(
            """INSERT INTO agent_plan_proposals (
                id, portfolio_id, choice_id, rank_index, status, brief_json,
                snapshot_json, score_json, verifier_json, evidence_json,
                canonical_signature, generation_lineage_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'offered', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                proposal.proposal_id,
                portfolio_id,
                choice_id,
                count,
                self._dump(proposal.brief.model_dump(by_alias=True)),
                self._dump(truthful_snapshot),
                self._dump(proposal.score.model_dump(by_alias=True)),
                self._dump(proposal.verifier),
                self._dump(
                    {
                        "grounded": proposal.grounded_evidence,
                        "unresolved": proposal.unresolved_evidence,
                    }
                ),
                proposal.canonical_signature,
                self._dump(proposal.generation_lineage),
                now,
                now,
            ),
        )
        try:
            summary = json.loads(row["summary_json"] or "{}")
        except (TypeError, ValueError):
            summary = {}
        proposal_ids = [str(item) for item in summary.get("proposalIds") or [] if str(item)]
        visible_ids = [str(item) for item in summary.get("visibleProposalIds") or [] if str(item)]
        if proposal.proposal_id not in proposal_ids:
            proposal_ids.append(proposal.proposal_id)
        if proposal.proposal_id not in visible_ids and len(visible_ids) < self.max_visible_comparison_cards:
            visible_ids.append(proposal.proposal_id)
        summary.update(
            {
                "proposalIds": proposal_ids,
                "visibleProposalIds": visible_ids,
                "repairedProposalId": proposal.proposal_id,
            }
        )
        summary = self._sync_summary_identity(
            summary,
            portfolio_id=portfolio_id,
            planning_root_id=str(row["source_user_turn_id"] or ""),
            request_fingerprint=str(row["request_contract_fingerprint"] or ""),
            status="awaiting_selection",
        )
        self.db.execute(
            """UPDATE agent_plan_portfolios
            SET status = 'awaiting_selection', summary_json = ?,
                failure_reason = NULL, updated_at = ?
            WHERE id = ? AND status IN ('awaiting_selection', 'failed')""",
            (self._dump(summary), now, portfolio_id),
        )
        self._persist_classification_counts(portfolio_id)
        self.db.commit()
        return choice_id, proposal.proposal_id, True

    def update_brief_generation_state(
        self,
        *,
        portfolio_id: str,
        ordered_brief_ids: list[str],
        processed_brief_ids: list[str],
        completed_brief_ids: list[str],
        failure_reason_codes: dict[str, list[str]],
        superseded_brief_ids: Optional[list[str]] = None,
        result_types: Optional[dict[str, str]] = None,
        expected_source_user_turn_id: Optional[str] = None,
        expected_source_assistant_turn_id: Optional[str] = None,
        expected_request_contract_fingerprint: Optional[str] = None,
    ) -> dict[str, Any]:
        """Merge one bounded brief attempt into the root portfolio summary."""

        row = self.db.execute(
            """SELECT summary_json, status, expires_at, source_user_turn_id,
                source_assistant_turn_id, request_contract_fingerprint
            FROM agent_plan_portfolios WHERE id = ?""",
            (portfolio_id,),
        ).fetchone()
        if row is None:
            raise ValueError("plan_portfolio_not_found")
        status = str(row["status"] or "")
        if status not in {"building", "awaiting_selection", "failed"}:
            raise ValueError("plan_portfolio_not_updatable")
        if self._is_expired(row["expires_at"]):
            if status in {"awaiting_selection", "failed"}:
                self._expire_portfolio(portfolio_id, "plan_portfolio_expired")
            raise ValueError("plan_portfolio_expired")
        if expected_source_user_turn_id is not None and str(row["source_user_turn_id"] or "") != str(
            expected_source_user_turn_id
        ):
            raise ValueError("portfolio_source_user_turn_mismatch")
        if expected_source_assistant_turn_id is not None and str(row["source_assistant_turn_id"] or "") != str(
            expected_source_assistant_turn_id
        ):
            raise ValueError("portfolio_source_assistant_turn_mismatch")
        if expected_request_contract_fingerprint is not None and str(row["request_contract_fingerprint"] or "") != str(
            expected_request_contract_fingerprint
        ):
            raise ValueError("portfolio_request_fingerprint_mismatch")
        raw_summary_json = str(row["summary_json"] or "{}")
        try:
            summary = json.loads(raw_summary_json)
        except (TypeError, ValueError):
            summary = {}
        existing_rows = {
            str(item.get("briefId") or ""): dict(item)
            for item in summary.get("briefGenerationState") or []
            if isinstance(item, dict) and str(item.get("briefId") or "")
        }
        requested_order = self._strict_unique_ids(ordered_brief_ids, "brief_order")
        existing_order = [
            str(item.get("briefId") or "")
            for item in summary.get("briefGenerationState") or []
            if isinstance(item, dict) and str(item.get("briefId") or "")
        ]
        if existing_order and requested_order != existing_order:
            existing_set = set(existing_order)
            requested_existing = [item for item in requested_order if item in existing_set]
            new_ids = [item for item in requested_order if item not in existing_set]
            if requested_existing and requested_existing != [
                item for item in existing_order if item in set(requested_existing)
            ]:
                raise ValueError("portfolio_brief_order_mismatch")
            ordered = [*existing_order, *new_ids]
        else:
            ordered = existing_order or requested_order
        if len(ordered) > self.max_generated_proposals_per_root:
            raise ValueError("portfolio_root_generation_budget_exceeded")
        processed = set(self._strict_unique_ids(processed_brief_ids, "processed_briefs"))
        completed = set(self._strict_unique_ids(completed_brief_ids, "completed_briefs"))
        superseded = set(self._strict_unique_ids(superseded_brief_ids or [], "superseded_briefs"))
        if not processed.issubset(set(ordered)) or not completed.issubset(processed):
            raise ValueError("portfolio_brief_outcome_scope_mismatch")
        if not superseded.issubset(set(ordered)) or superseded & processed:
            raise ValueError("portfolio_brief_superseded_scope_mismatch")
        if not isinstance(failure_reason_codes, dict):
            raise ValueError("portfolio_failure_reason_payload_invalid")
        result_types = {} if result_types is None else result_types
        if not isinstance(result_types, dict):
            raise ValueError("portfolio_result_type_payload_invalid")
        if set(str(key) for key in failure_reason_codes) - set(ordered):
            raise ValueError("portfolio_failure_reason_scope_mismatch")
        if set(str(key) for key in result_types) - set(ordered):
            raise ValueError("portfolio_result_type_scope_mismatch")
        state_rows: list[dict[str, Any]] = []
        for index, brief_id in enumerate(ordered):
            previous = existing_rows.get(brief_id, {})
            previous_status = str(previous.get("status") or "remaining")
            if brief_id in completed or previous_status == "completed":
                state = "completed"
            elif brief_id in processed:
                state = "failed"
            elif brief_id in superseded or previous_status == "superseded":
                state = "superseded"
            elif previous_status in {"failed", "remaining"}:
                state = previous_status
            else:
                state = "remaining"
            raw_reason_codes = failure_reason_codes.get(brief_id, [])
            if not isinstance(raw_reason_codes, list):
                raise ValueError("portfolio_failure_reason_payload_invalid")
            reason_codes = self._strict_unique_ids(raw_reason_codes, "failure_reason_codes")
            if state != "failed":
                reason_codes = []
            row_payload: dict[str, Any] = {
                "briefId": brief_id,
                "order": index,
                "status": state,
                "reasonCodes": reason_codes,
            }
            result_type = (
                "" if state == "failed" else str(result_types.get(brief_id) or previous.get("resultType") or "")
            )
            if result_type:
                row_payload["resultType"] = result_type
            state_rows.append(row_payload)
        completed_ids = [item["briefId"] for item in state_rows if item["status"] == "completed"]
        remaining_ids = [item["briefId"] for item in state_rows if item["status"] == "remaining"]
        failed_ids = [item["briefId"] for item in state_rows if item["status"] == "failed"]
        superseded_ids = [item["briefId"] for item in state_rows if item["status"] == "superseded"]
        current_focus_brief_id = next(
            (item["briefId"] for item in reversed(state_rows) if item["status"] in {"completed", "failed"}),
            str(summary.get("focusBriefId") or ""),
        )
        summary.update(
            {
                "briefGenerationState": state_rows,
                "completedBriefIds": completed_ids,
                "remainingBriefIds": remaining_ids,
                "failedBriefIds": failed_ids,
                "supersededBriefIds": superseded_ids,
                "nextBriefId": next(iter(remaining_ids), ""),
                "focusBriefId": current_focus_brief_id,
            }
        )
        summary = self._sync_summary_identity(
            summary,
            portfolio_id=portfolio_id,
            planning_root_id=str(row["source_user_turn_id"] or ""),
            request_fingerprint=str(row["request_contract_fingerprint"] or ""),
            status=status,
        )
        frontier = summary.get("creativeExplorationFrontier")
        if isinstance(frontier, dict):
            frontier.update(
                {
                    "planningSelectionRootTurnId": str(row["source_user_turn_id"] or ""),
                    "rootPortfolioId": portfolio_id,
                    "requestContractFingerprint": str(row["request_contract_fingerprint"] or ""),
                    "nextBriefId": str(summary["nextBriefId"] or ""),
                    "currentFocusBriefId": current_focus_brief_id,
                }
            )
        updated = self.db.execute(
            """UPDATE agent_plan_portfolios SET summary_json = ?, updated_at = ?
            WHERE id = ? AND status IN ('building', 'awaiting_selection', 'failed')
              AND summary_json = ?""",
            (self._dump(summary), self._now(), portfolio_id, raw_summary_json),
        )
        if updated.rowcount != 1:
            self.db.rollback()
            raise ValueError("portfolio_brief_generation_state_stale")
        self.db.commit()
        return summary

    def update_exploration_frontier(
        self,
        *,
        portfolio_id: str,
        frontier: dict[str, Any],
        expected_request_contract_fingerprint: str,
    ) -> dict[str, Any]:
        """Persist one normalized frontier in summary_json with a stale-write guard."""
        row = self.db.execute(
            """SELECT summary_json, request_contract_fingerprint, status,
                source_user_turn_id
            FROM agent_plan_portfolios WHERE id = ?""",
            (portfolio_id,),
        ).fetchone()
        if row is None:
            raise ValueError("plan_portfolio_not_found")
        if str(row["request_contract_fingerprint"] or "") != expected_request_contract_fingerprint:
            raise ValueError("portfolio_request_fingerprint_mismatch")
        raw_summary = str(row["summary_json"] or "{}")
        try:
            summary = json.loads(raw_summary)
        except (TypeError, ValueError):
            summary = {}
        if not isinstance(frontier, dict):
            raise ValueError("creative_exploration_frontier_invalid")
        next_brief_id = str(summary.get("nextBriefId") or "")
        if not CreativeExplorationFrontierService.can_continue(frontier):
            next_brief_id = ""
            summary["nextBriefId"] = ""
        normalized_frontier = copy.deepcopy(frontier)
        normalized_frontier.update(
            {
                "planningSelectionRootTurnId": str(row["source_user_turn_id"] or ""),
                "rootPortfolioId": portfolio_id,
                "requestContractFingerprint": str(row["request_contract_fingerprint"] or ""),
                "nextBriefId": next_brief_id,
            }
        )
        summary["creativeExplorationFrontier"] = normalized_frontier
        summary = self._sync_summary_identity(
            summary,
            portfolio_id=portfolio_id,
            planning_root_id=str(row["source_user_turn_id"] or ""),
            request_fingerprint=str(row["request_contract_fingerprint"] or ""),
            status=str(row["status"] or ""),
        )
        updated = self.db.execute(
            """UPDATE agent_plan_portfolios SET summary_json = ?, updated_at = ?
            WHERE id = ? AND summary_json = ?""",
            (self._dump(summary), self._now(), portfolio_id, raw_summary),
        )
        if updated.rowcount != 1:
            self.db.rollback()
            raise ValueError("creative_exploration_frontier_stale")
        self.db.commit()
        return summary

    def reserve_candidate_amap_budget(
        self,
        *,
        portfolio_id: str,
        execution_id: str,
        expected_request_contract_fingerprint: str,
    ) -> dict[str, Any]:
        """Persist a root-scoped candidate budget claim before AMap work."""

        return self._mutate_candidate_amap_budget(
            portfolio_id=portfolio_id,
            execution_id=execution_id,
            expected_request_contract_fingerprint=expected_request_contract_fingerprint,
            actual_usage=None,
        )

    def settle_candidate_amap_budget(
        self,
        *,
        portfolio_id: str,
        execution_id: str,
        actual_usage: dict[str, Any],
        expected_request_contract_fingerprint: str,
    ) -> dict[str, Any]:
        """Settle one claimed candidate budget with actual external calls."""

        return self._mutate_candidate_amap_budget(
            portfolio_id=portfolio_id,
            execution_id=execution_id,
            expected_request_contract_fingerprint=expected_request_contract_fingerprint,
            actual_usage=actual_usage,
        )

    def _mutate_candidate_amap_budget(
        self,
        *,
        portfolio_id: str,
        execution_id: str,
        expected_request_contract_fingerprint: str,
        actual_usage: Optional[dict[str, Any]],
    ) -> dict[str, Any]:
        row = self.db.execute(
            """SELECT summary_json, request_contract_fingerprint, status,
                source_user_turn_id
            FROM agent_plan_portfolios WHERE id = ?""",
            (portfolio_id,),
        ).fetchone()
        if row is None:
            raise ValueError("plan_portfolio_not_found")
        if str(row["request_contract_fingerprint"] or "") != expected_request_contract_fingerprint:
            raise ValueError("portfolio_request_fingerprint_mismatch")
        raw_summary = str(row["summary_json"] or "{}")
        try:
            summary = json.loads(raw_summary)
        except (TypeError, ValueError):
            summary = {}
        frontier = summary.get("creativeExplorationFrontier")
        if not isinstance(frontier, dict):
            raise ValueError("creative_exploration_frontier_missing")
        service = CreativeExplorationFrontierService()
        if actual_usage is None:
            result = service.reserve_candidate_amap_budget(
                frontier,
                execution_id=execution_id,
            )
        else:
            result = service.settle_candidate_amap_budget(
                frontier,
                execution_id=execution_id,
                actual_usage=actual_usage,
            )
        mutated_frontier = result.get("frontier")
        if not isinstance(mutated_frontier, dict):
            raise ValueError("creative_exploration_frontier_invalid")
        if mutated_frontier == frontier:
            return {**result, "frontier": copy.deepcopy(frontier), "summary": summary}
        normalized_frontier = copy.deepcopy(mutated_frontier)
        next_brief_id = str(summary.get("nextBriefId") or "")
        if not CreativeExplorationFrontierService.can_continue(normalized_frontier):
            next_brief_id = ""
            summary["nextBriefId"] = ""
        normalized_frontier.update(
            {
                "planningSelectionRootTurnId": str(row["source_user_turn_id"] or ""),
                "rootPortfolioId": portfolio_id,
                "requestContractFingerprint": str(row["request_contract_fingerprint"] or ""),
                "nextBriefId": next_brief_id,
            }
        )
        summary["creativeExplorationFrontier"] = normalized_frontier
        summary = self._sync_summary_identity(
            summary,
            portfolio_id=portfolio_id,
            planning_root_id=str(row["source_user_turn_id"] or ""),
            request_fingerprint=str(row["request_contract_fingerprint"] or ""),
            status=str(row["status"] or ""),
        )
        updated = self.db.execute(
            """UPDATE agent_plan_portfolios SET summary_json = ?, updated_at = ?
            WHERE id = ? AND summary_json = ?""",
            (self._dump(summary), self._now(), portfolio_id, raw_summary),
        )
        if updated.rowcount != 1:
            self.db.rollback()
            raise ValueError("creative_exploration_frontier_stale")
        self.db.commit()
        return {**result, "frontier": normalized_frontier, "summary": summary}

    def claim_night_view_query(
        self,
        *,
        portfolio_id: str,
        expected_request_contract_fingerprint: str,
        scope: dict[str, Any],
        semantic_fingerprint: str,
        queries: list[dict[str, Any]],
        attempt_identity: str,
    ) -> dict[str, Any]:
        """Persist one exact night-query claim before the provider boundary.

        The nested ledger remains part of ``creativeExplorationFrontier``.  This
        deliberately reuses the Portfolio root's stale-write guard instead of
        creating a second cache or table with a competing cursor truth.
        """

        return self._mutate_night_view_query_progress(
            portfolio_id=portfolio_id,
            expected_request_contract_fingerprint=expected_request_contract_fingerprint,
            scope=scope,
            semantic_fingerprint=semantic_fingerprint,
            queries=queries,
            attempt_identity=attempt_identity,
            provider_completed=None,
            provider_receipt_fingerprint=None,
        )

    def complete_night_view_query(
        self,
        *,
        portfolio_id: str,
        expected_request_contract_fingerprint: str,
        scope: dict[str, Any],
        semantic_fingerprint: str,
        queries: list[dict[str, Any]],
        attempt_identity: str,
        provider_completed: bool,
        provider_receipt_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        """Persist a safe receipt after a claimed night query actually completed."""

        return self._mutate_night_view_query_progress(
            portfolio_id=portfolio_id,
            expected_request_contract_fingerprint=expected_request_contract_fingerprint,
            scope=scope,
            semantic_fingerprint=semantic_fingerprint,
            queries=queries,
            attempt_identity=attempt_identity,
            provider_completed=provider_completed,
            provider_receipt_fingerprint=provider_receipt_fingerprint,
        )

    def _mutate_night_view_query_progress(
        self,
        *,
        portfolio_id: str,
        expected_request_contract_fingerprint: str,
        scope: dict[str, Any],
        semantic_fingerprint: str,
        queries: list[dict[str, Any]],
        attempt_identity: str,
        provider_completed: bool | None,
        provider_receipt_fingerprint: str | None,
    ) -> dict[str, Any]:
        """Apply one pure frontier transition with bounded local CAS reconciliation.

        Retrying this compare-and-swap is safe because no Provider work happens
        here.  A stale contender reloads the persisted claim and receives an
        ``IN_FLIGHT``/``REPLAY`` result rather than gaining a second claim.
        """

        for _ in range(2):
            row = self.db.execute(
                """SELECT summary_json, request_contract_fingerprint, source_user_turn_id, status
                   FROM agent_plan_portfolios WHERE id = ?""",
                (portfolio_id,),
            ).fetchone()
            if row is None:
                raise ValueError("plan_portfolio_not_found")
            if str(row["request_contract_fingerprint"] or "") != str(expected_request_contract_fingerprint or ""):
                raise ValueError("portfolio_request_fingerprint_mismatch")
            if str(scope.get("rootPortfolioId") or "") != portfolio_id or str(scope.get("planningRoot") or "") != str(
                row["source_user_turn_id"] or ""
            ):
                raise ValueError("night_view_progress_portfolio_scope_mismatch")
            raw_summary = str(row["summary_json"] or "{}")
            try:
                summary = json.loads(raw_summary)
            except (TypeError, ValueError) as error:
                raise ValueError("creative_exploration_frontier_invalid") from error
            frontier = summary.get("creativeExplorationFrontier")
            if not isinstance(frontier, dict):
                raise ValueError("creative_exploration_frontier_invalid")
            if provider_completed is None:
                result = CreativeExplorationFrontierService.claim_night_view_query(
                    frontier,
                    scope=scope,
                    semantic_fingerprint=semantic_fingerprint,
                    queries=queries,
                    attempt_identity=attempt_identity,
                )
            else:
                result = CreativeExplorationFrontierService.complete_night_view_query(
                    frontier,
                    scope=scope,
                    semantic_fingerprint=semantic_fingerprint,
                    queries=queries,
                    attempt_identity=attempt_identity,
                    provider_completed=provider_completed,
                    provider_receipt_fingerprint=provider_receipt_fingerprint,
                )
            next_frontier = result.get("frontier")
            if not isinstance(next_frontier, dict):
                raise ValueError("night_view_progress_transition_invalid")
            # A replay, a competing in-flight attempt, or terminal NO_PROGRESS
            # must be a read-only answer.  In particular, do not bump the
            # Portfolio row's timestamp for an idempotent retry: the existing
            # frontier is the only authority and its cursor is unchanged.
            if next_frontier == frontier:
                return result
            summary["creativeExplorationFrontier"] = next_frontier
            summary = self._sync_summary_identity(
                summary,
                portfolio_id=portfolio_id,
                planning_root_id=str(row["source_user_turn_id"] or ""),
                request_fingerprint=str(row["request_contract_fingerprint"] or ""),
                status=str(row["status"] or ""),
            )
            updated = self.db.execute(
                """UPDATE agent_plan_portfolios SET summary_json = ?, updated_at = ?
                   WHERE id = ? AND summary_json = ?""",
                (self._dump(summary), self._now(), portfolio_id, raw_summary),
            )
            if updated.rowcount == 1:
                self.db.commit()
                return result
            self.db.rollback()
        raise ValueError("creative_exploration_frontier_stale")

    def mark_route_degraded_continuable(
        self,
        *,
        portfolio_id: str,
        expected_request_contract_fingerprint: str,
        expected_next_brief_id: str,
    ) -> Optional[dict[str, Any]]:
        """Keep a zero-proposal route failure nonterminal while work remains."""

        return self.mark_degraded_continuable(
            portfolio_id=portfolio_id,
            expected_request_contract_fingerprint=(expected_request_contract_fingerprint),
            expected_next_brief_id=expected_next_brief_id,
            generation_state="route_degraded",
            require_route_failure=True,
        )

    def mark_degraded_continuable(
        self,
        *,
        portfolio_id: str,
        expected_request_contract_fingerprint: str,
        expected_next_brief_id: str,
        generation_state: str,
        require_route_failure: bool = False,
    ) -> Optional[dict[str, Any]]:
        """Keep a root nonterminal when its persisted frontier still has work."""

        if generation_state not in {"route_degraded", "degraded_has_more"}:
            raise ValueError("portfolio_generation_state_invalid")

        row = self.db.execute(
            """SELECT summary_json, status, failure_reason, expires_at,
                request_contract_fingerprint, source_user_turn_id
            FROM agent_plan_portfolios WHERE id = ?""",
            (portfolio_id,),
        ).fetchone()
        if row is None:
            raise ValueError("plan_portfolio_not_found")
        if str(row["request_contract_fingerprint"] or "") != str(expected_request_contract_fingerprint or ""):
            raise ValueError("portfolio_request_fingerprint_mismatch")
        if self._is_expired(row["expires_at"]):
            self._expire_portfolio(portfolio_id, "plan_portfolio_expired")
            return None
        raw_summary = str(row["summary_json"] or "{}")
        try:
            summary = json.loads(raw_summary)
        except (TypeError, ValueError):
            summary = {}
        frontier = summary.get("creativeExplorationFrontier")
        next_brief_id = str(summary.get("nextBriefId") or "")
        if (
            str(row["status"] or "") not in {"failed", "awaiting_selection"}
            or (
                require_route_failure
                and not str(row["failure_reason"] or "").startswith("portfolio_route_quality_unresolved:")
            )
            or not isinstance(frontier, dict)
            or not CreativeExplorationFrontierService.can_continue(frontier)
            or not (next_brief_id or str(frontier.get("currentFocusBriefId") or ""))
            or next_brief_id != str(expected_next_brief_id or "")
            or bool(summary.get("visibleProposalIds"))
        ):
            return None
        if str(row["status"] or "") == "awaiting_selection" and summary.get("generationState") == generation_state:
            return summary
        summary.update(
            {
                "generationState": generation_state,
                "continuationStatus": str(frontier.get("frontierState") or "has_more"),
                "routeDegraded": generation_state == "route_degraded",
            }
        )
        frontier.update(
            {
                "planningSelectionRootTurnId": str(row["source_user_turn_id"] or ""),
                "rootPortfolioId": portfolio_id,
                "requestContractFingerprint": str(row["request_contract_fingerprint"] or ""),
                "nextBriefId": next_brief_id,
            }
        )
        summary["creativeExplorationFrontier"] = frontier
        summary = self._sync_summary_identity(
            summary,
            portfolio_id=portfolio_id,
            planning_root_id=str(row["source_user_turn_id"] or ""),
            request_fingerprint=str(row["request_contract_fingerprint"] or ""),
            status="awaiting_selection",
        )
        updated = self.db.execute(
            """UPDATE agent_plan_portfolios
            SET status = 'awaiting_selection', summary_json = ?, updated_at = ?
            WHERE id = ? AND status IN ('failed', 'awaiting_selection')
              AND summary_json = ?""",
            (self._dump(summary), self._now(), portfolio_id, raw_summary),
        )
        if updated.rowcount != 1:
            self.db.rollback()
            raise ValueError("portfolio_route_degraded_state_stale")
        self.db.commit()
        return summary

    @staticmethod
    def _sync_summary_identity(
        summary: dict[str, Any],
        *,
        portfolio_id: str,
        planning_root_id: str,
        request_fingerprint: str,
        status: str,
    ) -> dict[str, Any]:
        normalized = copy.deepcopy(summary) if isinstance(summary, dict) else {}
        normalized.update(
            {
                "status": str(status or ""),
                "planningSelectionRootTurnId": str(planning_root_id or ""),
                "rootPortfolioId": str(portfolio_id or ""),
                "requestContractFingerprint": str(request_fingerprint or ""),
            }
        )
        return normalized

    @staticmethod
    def _strict_unique_ids(values: Any, field: str) -> list[str]:
        if not isinstance(values, list):
            raise ValueError(f"portfolio_{field}_payload_invalid")
        normalized: list[str] = []
        seen: set[str] = set()
        for raw in values:
            value = str(raw or "").strip()
            if not value:
                raise ValueError(f"portfolio_{field}_payload_invalid")
            if value in seen:
                raise ValueError(f"portfolio_{field}_duplicate")
            seen.add(value)
            normalized.append(value)
        return normalized

    def claim_selection(self, *, portfolio_id: str, proposal_id: str) -> bool:
        updated = self.db.execute(
            """UPDATE agent_plan_portfolios SET status = 'committing', selected_proposal_id = ?, updated_at = ?
            WHERE id = ? AND status IN ('awaiting_selection', 'failed')""",
            (proposal_id, self._now(), portfolio_id),
        )
        self.db.commit()
        return updated.rowcount == 1

    def mark_committed(
        self,
        *,
        portfolio_id: str,
        proposal_id: str,
        result_version_id: Optional[str] = None,
    ) -> None:
        self._persist_committed_selection(
            portfolio_id=portfolio_id,
            proposal_id=proposal_id,
            result_version_id=result_version_id,
        )

    def reconcile_committed_selection(
        self,
        *,
        portfolio_id: str,
        proposal_id: str,
        result_version_id: str,
    ) -> None:
        """Complete post-writer bookkeeping from authoritative persisted evidence.

        The canonical itinerary writer commits before proposal bookkeeping.  If
        that final bookkeeping call faults, retrying the opaque choice must not
        create a second version.  Recovery is allowed only when the exact result
        version is still this portfolio session's active version and the CAS
        still names the same proposal.
        """

        identity = self.db.execute(
            """SELECT p.session_id, p.status, p.selected_proposal_id,
                      s.active_version_id
               FROM agent_plan_portfolios p
               JOIN conversation_sessions s ON s.id = p.session_id
               WHERE p.id = ?""",
            (portfolio_id,),
        ).fetchone()
        proposal_exists = self.db.execute(
            "SELECT 1 FROM agent_plan_proposals WHERE id = ? AND portfolio_id = ?",
            (proposal_id, portfolio_id),
        ).fetchone()
        version_exists = (
            self.db.execute(
                "SELECT 1 FROM itinerary_versions WHERE id = ? AND session_id = ?",
                (result_version_id, str(identity["session_id"]) if identity is not None else ""),
            ).fetchone()
            if identity is not None
            else None
        )
        if (
            identity is None
            or proposal_exists is None
            or version_exists is None
            or str(identity["selected_proposal_id"] or "") != str(proposal_id)
            or str(identity["active_version_id"] or "") != str(result_version_id)
            or str(identity["status"] or "") not in {"committing", "awaiting_selection", "committed"}
        ):
            raise ValueError("plan_proposal_commit_recovery_identity_mismatch")
        self._persist_committed_selection(
            portfolio_id=portfolio_id,
            proposal_id=proposal_id,
            result_version_id=result_version_id,
        )

    def _persist_committed_selection(
        self,
        *,
        portfolio_id: str,
        proposal_id: str,
        result_version_id: Optional[str] = None,
    ) -> None:
        now = self._now()
        row = self.db.execute(
            "SELECT summary_json FROM agent_plan_portfolios WHERE id = ?",
            (portfolio_id,),
        ).fetchone()
        try:
            summary = json.loads(str(row["summary_json"] or "{}")) if row is not None else {}
        except (TypeError, ValueError):
            summary = {}
        if not isinstance(summary, dict):
            summary = {}
        simple_direction = str(summary.get("workflowMode") or "") == "simple_direction_v1"
        terminal_status = "awaiting_selection" if simple_direction else "committed"
        summary.update(
            {
                "selectedProposalId": proposal_id,
                "status": terminal_status,
                **({"expectedBaseVersionId": str(result_version_id or "")} if simple_direction else {}),
            }
        )
        proposal_row = self.db.execute(
            "SELECT evidence_json FROM agent_plan_proposals WHERE id = ? AND portfolio_id = ?",
            (proposal_id, portfolio_id),
        ).fetchone()
        try:
            evidence = json.loads(str(proposal_row["evidence_json"] or "{}")) if proposal_row is not None else {}
        except (TypeError, ValueError):
            evidence = {}
        if not isinstance(evidence, dict):
            evidence = {}
        readiness = evidence.get("readiness")
        if isinstance(readiness, dict):
            adopted_with_soft_pending = bool(
                readiness.get("draftAdoptionReady") is True
                and int(readiness.get("hardPendingSlotCount") or 0) == 0
                and int(readiness.get("softPendingSlotCount") or readiness.get("pendingSlotCount") or 0) > 0
            )
            readiness.update(
                {
                    "isAdopted": True,
                    "nextAction": ("complete_pending_slots" if adopted_with_soft_pending else "none"),
                    "nextActionLabel": ("继续补充软体验" if adopted_with_soft_pending else "已采用"),
                }
            )
        self.db.execute(
            "UPDATE agent_plan_portfolios SET status = ?, selected_proposal_id = ?, "
            "expected_base_version_id = CASE WHEN ? THEN ? ELSE expected_base_version_id END, "
            "summary_json = ?, updated_at = ? WHERE id = ?",
            (
                terminal_status,
                proposal_id,
                1 if simple_direction else 0,
                str(result_version_id or ""),
                self._dump(summary),
                now,
                portfolio_id,
            ),
        )
        self.db.execute(
            "UPDATE agent_plan_proposals SET status = CASE WHEN id = ? THEN 'committed' ELSE 'comparison_only' END, updated_at = ? WHERE portfolio_id = ?",
            (proposal_id, now, portfolio_id),
        )
        if proposal_row is not None:
            self.db.execute(
                "UPDATE agent_plan_proposals SET evidence_json = ? WHERE id = ? AND portfolio_id = ?",
                (self._dump(evidence), proposal_id, portfolio_id),
            )
        self.db.commit()

    def release_selection(
        self,
        *,
        portfolio_id: str,
        reason: str,
        restore_proposal_id: Optional[str] = None,
    ) -> None:
        """Fail closed without consuming the proposal when pre-write verification fails."""
        self.db.execute(
            "UPDATE agent_plan_portfolios SET status = 'awaiting_selection', selected_proposal_id = ?, failure_reason = ?, updated_at = ? WHERE id = ? AND status = 'committing'",
            (restore_proposal_id, reason, self._now(), portfolio_id),
        )
        self.db.commit()

    def release_stale_selection(self, *, portfolio_id: str, reason: str) -> bool:
        """Release only a timed-out claim with no writer evidence.

        `committing` is a lease rather than a permanent terminal state.  This
        protects against a process death after CAS but before the itinerary
        writer starts, while leaving a live concurrent commit untouched.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=self.SELECTION_LEASE_SECONDS)).isoformat()
        updated = self.db.execute(
            """UPDATE agent_plan_portfolios
            SET status = 'awaiting_selection', selected_proposal_id = NULL,
                failure_reason = ?, updated_at = ?
            WHERE id = ? AND status = 'committing' AND updated_at <= ?""",
            (reason, self._now(), portfolio_id, cutoff),
        )
        self.db.commit()
        return updated.rowcount == 1

    def expire_session(self, session_id: str, reason: str) -> int:
        rows = self.db.execute(
            "SELECT id FROM agent_plan_portfolios WHERE session_id = ? AND status = 'awaiting_selection'",
            (session_id,),
        ).fetchall()
        for row in rows:
            self._expire_portfolio(str(row["id"]), reason, commit=False)
        self.db.commit()
        return len(rows)

    def _expire_portfolio(self, portfolio_id: str, reason: str, *, commit: bool = True) -> None:
        now = self._now()
        self.db.execute(
            """UPDATE agent_plan_portfolios SET status = 'expired', failure_reason = ?, updated_at = ?
            WHERE id = ? AND status IN ('awaiting_selection', 'failed')""",
            (reason, now, portfolio_id),
        )
        self.db.execute(
            "UPDATE agent_plan_proposals SET status = 'expired', updated_at = ? WHERE portfolio_id = ? AND status = 'offered'",
            (now, portfolio_id),
        )
        if commit:
            self.db.commit()

    @staticmethod
    def _is_expired(value: Any) -> bool:
        if not value:
            return False
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")) <= datetime.now(timezone.utc)
        except ValueError:
            return True

    @staticmethod
    def _choice_id(proposal: PlanCandidate) -> str:
        return f"portfolio_choice_{proposal.proposal_id}"

    @staticmethod
    def _dump(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)

    @staticmethod
    def simple_direction_compatibility_attempt_fingerprint(value: dict[str, Any]) -> str:
        schema_version = str(value.get("schemaVersion") or "")
        fields = [
            "schemaVersion",
            "executionId",
            "sessionId",
            "planningSelectionRootTurnId",
            "rootPortfolioId",
            "sourceAssistantTurnId",
            "requestTurnId",
            "choiceId",
            "requestContractFingerprint",
        ]
        if schema_version in {
            "simple-direction-compatibility-attempt-v2",
            "simple-direction-compatibility-attempt-v3",
        }:
            fields.extend(("providerPage", "pageOffset", "maxPagesPerQuery"))
        if schema_version == "simple-direction-compatibility-attempt-v3":
            fields.extend(("slotQueries", "slotFrontierSnapshot"))
        identity_material = {key: value.get(key) for key in fields}
        return hashlib.sha256(
            json.dumps(
                identity_material,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def simple_direction_compatibility_result_fingerprint(value: dict[str, Any]) -> str:
        result_material = {
            key: value.get(key)
            for key in (
                "resultAssistantTurnId",
                "proposalId",
                "proposalDelta",
                "disposition",
                "status",
                "frontierStatus",
                "reasonCode",
                "progress",
                "versionDelta",
                "patchDelta",
                "routeWriteDelta",
            )
        }
        return hashlib.sha256(
            json.dumps(
                result_material,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _model_payload(value: Any) -> Any:
        if hasattr(value, "model_dump"):
            return value.model_dump(by_alias=True)
        return value

    @staticmethod
    def _truthful_snapshot_title(
        snapshot: dict[str, Any],
        verifier: dict[str, Any],
    ) -> dict[str, Any]:
        material = copy.deepcopy(snapshot)
        readiness = ProposalReadinessService.compute(
            material,
            verifier=verifier,
        )
        if readiness.get("strictlyVerified") is not True:
            material["title"] = CreativeProposalTitleService.incomplete_status_title(
                material,
                verifier,
            )
            material.pop("portfolioTitleEvidence", None)
        return material

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

"""Bounded, persistent root-level Creative Portfolio exploration frontier."""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from typing import Any
import unicodedata

from src.services.amap_call_budget import AmapCallBudget


_NIGHT_VIEW_PROGRESS_SCHEMA = "trip-night-view-query-progress-v1"
_NIGHT_SCOPE_FIELDS = (
    "tenantId",
    "userId",
    "planningRoot",
    "rootPortfolioId",
    "briefId",
    "poolId",
    "planningSlotId",
    "dayNumber",
    "timeWindow",
)
_NIGHT_SEMANTIC_SCOPE_FIELDS = (
    "briefId",
    "poolId",
    "planningSlotId",
    "dayNumber",
    "timeWindow",
)
_NIGHT_ATTEMPT_ID = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_NIGHT_SEMANTIC_GOAL_FIELDS = frozenset(
    {"goalId", "softGoalId", "rawNeed", "intentType", "experienceGoal"}
)
_NIGHT_SEMANTIC_EXPERIENCE_FIELDS = frozenset(
    {
        "experienceFamily",
        "semanticFacets",
        "desiredSignals",
        "avoidSignals",
        "evidencePolicy",
        "groundingPolicy",
    }
)
_NIGHT_NON_SEMANTIC_FIELD_NAMES = frozenset(
    {
        "sessionId",
        "turnId",
        "userTurnId",
        "currentUserTurnId",
        "sourceUserTurnId",
        "sourceAssistantTurnId",
        "planningRoot",
        "planningSelectionRootTurnId",
        "requestId",
        "traceId",
        "auditId",
        "auditFingerprint",
        "timestamp",
        "createdAt",
        "updatedAt",
        "issuedAt",
        "expiresAt",
        "generatedAt",
        "randomSeed",
        "seed",
        "nonce",
        "uuid",
        "displayId",
    }
)
_NIGHT_NON_SEMANTIC_FIELD_ALIASES = frozenset(
    re.sub(r"[^a-z0-9]", "", field.casefold())
    for field in _NIGHT_NON_SEMANTIC_FIELD_NAMES
)
_MAX_NIGHT_VIEW_PROGRESS_SCOPES = 64
_MAX_NIGHT_VIEW_QUERIES = 24
_MAX_NIGHT_VIEW_ATTEMPTS = 128
_CANDIDATE_BUDGET_EXECUTION_ID = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
_MAX_CANDIDATE_BUDGET_RESERVATIONS = 32


class CreativeExplorationFrontierService:
    CONTINUABLE_STATES = frozenset({"ready", "has_more", "temporarily_degraded", "route_degraded"})

    def __init__(
        self,
        *,
        max_visible_comparison_cards: int = 6,
        max_generated_proposals_per_root: int = 12,
        max_continuation_rounds_per_root: int = 4,
        max_provider_calls_per_root: int = 4,
        max_amap_calls_per_continuation: int = 8,
        max_amap_candidate_text_calls_per_root: int = 12,
        max_amap_candidate_around_calls_per_root: int = 8,
        max_amap_candidate_calls_per_root: int = 24,
        max_route_calls_per_continuation: int = 8,
        max_duplicate_repair_attempts: int = 2,
    ) -> None:
        self.limits = {
            "maxProposalsPerProviderCall": 4,
            "maxVisibleComparisonCards": max(1, max_visible_comparison_cards),
            "maxGeneratedProposalsPerRoot": max(1, max_generated_proposals_per_root),
            "maxContinuationRoundsPerRoot": max(1, max_continuation_rounds_per_root),
            "maxProviderCallsPerRoot": max(1, max_provider_calls_per_root),
            "maxAmapCallsPerContinuation": max(1, max_amap_calls_per_continuation),
            "maxAmapCandidateTextCallsPerRoot": max(1, max_amap_candidate_text_calls_per_root),
            "maxAmapCandidateAroundCallsPerRoot": max(1, max_amap_candidate_around_calls_per_root),
            "maxAmapCandidateCallsPerRoot": max(1, max_amap_candidate_calls_per_root),
            "maxRouteCallsPerContinuation": max(1, max_route_calls_per_continuation),
            "maxDuplicateRepairAttempts": max(0, max_duplicate_repair_attempts),
        }

    def initial(self, *, planning_root_id: str, portfolio_id: str, fingerprint: str) -> dict[str, Any]:
        return {
            "planningSelectionRootTurnId": planning_root_id,
            "rootPortfolioId": portfolio_id,
            "requestContractFingerprint": fingerprint,
            "generatedDirectionSignatures": [],
            "attemptedDirectionSignatures": [],
            "acceptedDirectionSignatures": [],
            "rejectedDirectionSignatures": [],
            "usedPrimaryAxes": [],
            "usedExperienceFamilySets": [],
            "usedDayRoleSignatures": [],
            "usedSearchProfileFingerprints": [],
            "visiblePhysicalPoiIds": [],
            "rejectedPhysicalPoiIds": [],
            "processedExecutionIds": [],
            "continuationRound": 0,
            "acceptedProposalCount": 0,
            "consecutiveFailedAttempts": 0,
            "providerCallCount": 0,
            "amapCandidateUsage": self._empty_amap_candidate_usage(),
            "amapCandidateRemaining": self._amap_candidate_remaining({}, self.limits),
            "remainingBudget": self.limits["maxGeneratedProposalsPerRoot"],
            "frontierState": "ready",
            "exhaustionReason": None,
            "limits": copy.deepcopy(self.limits),
            "updatedAt": self._now(),
        }

    def advance(
        self,
        frontier: dict[str, Any],
        *,
        execution_id: str,
        attempted_direction_signatures: list[str],
        accepted_direction_signatures: list[str],
        rejected_direction_signatures: list[str] | None = None,
        provider_called: bool,
        amap_candidate_usage: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        current = copy.deepcopy(frontier)
        processed = self._unique(current.get("processedExecutionIds") or [])
        if execution_id and execution_id in processed:
            return current
        attempted = self._merge(current.get("attemptedDirectionSignatures"), attempted_direction_signatures)
        accepted = self._merge(current.get("acceptedDirectionSignatures"), accepted_direction_signatures)
        rejected = self._merge(current.get("rejectedDirectionSignatures"), rejected_direction_signatures or [])
        generated = self._merge(current.get("generatedDirectionSignatures"), attempted_direction_signatures)
        if execution_id:
            processed.append(execution_id)
        limits = {**self.limits, **dict(current.get("limits") or {})}
        maximum = int(limits.get("maxGeneratedProposalsPerRoot") or 0)
        rounds = int(current.get("continuationRound") or 0) + 1
        provider_calls = int(current.get("providerCallCount") or 0) + int(provider_called)
        # Formal portfolio capacity is consumed only by proposals that survived
        # grounding, admission, novelty and verifier gates.  A rejected search
        # direction is an internal attempt, not a user-visible proposal.
        accepted_count = len(accepted)
        remaining = max(0, maximum - accepted_count)
        amap_usage = self._merge_amap_candidate_usage(
            current.get("amapCandidateUsage"),
            amap_candidate_usage,
        )
        amap_remaining = self._amap_candidate_remaining(amap_usage, limits)
        accepted_delta = len(
            set(str(item) for item in accepted_direction_signatures if str(item))
            - set(str(item) for item in current.get("acceptedDirectionSignatures") or [] if str(item))
        )
        consecutive_failures = 0 if accepted_delta > 0 else int(current.get("consecutiveFailedAttempts") or 0) + 1
        exhaustion_reason = None
        if remaining == 0:
            state = "budget_exhausted"
            exhaustion_reason = "max_generated_proposals_per_root"
        elif int(amap_remaining["remainingTotalExternal"]) == 0 or (
            int(amap_remaining["remainingPlaceTextAndDetail"]) == 0
            and int(amap_remaining["remainingPlaceAround"]) == 0
        ):
            state = "budget_exhausted"
            exhaustion_reason = "max_amap_candidate_calls_per_root"
        elif consecutive_failures >= int(limits.get("maxContinuationRoundsPerRoot") or 0):
            state = "temporarily_degraded"
            exhaustion_reason = "max_consecutive_failed_attempts"
        elif provider_calls >= int(limits.get("maxProviderCallsPerRoot") or 0):
            state = "temporarily_degraded"
            exhaustion_reason = "max_provider_calls_per_root"
        else:
            state = "has_more"
        current.update(
            {
                "generatedDirectionSignatures": generated,
                "attemptedDirectionSignatures": attempted,
                "acceptedDirectionSignatures": accepted,
                "rejectedDirectionSignatures": rejected,
                "processedExecutionIds": processed,
                "continuationRound": rounds,
                "acceptedProposalCount": accepted_count,
                "consecutiveFailedAttempts": consecutive_failures,
                "providerCallCount": provider_calls,
                "amapCandidateUsage": amap_usage,
                "amapCandidateRemaining": amap_remaining,
                "remainingBudget": remaining,
                "frontierState": state,
                "exhaustionReason": exhaustion_reason,
                "limits": limits,
                "updatedAt": self._now(),
            }
        )
        return current

    @classmethod
    def is_continuable_state(cls, state: Any) -> bool:
        return str(state or "") in cls.CONTINUABLE_STATES

    @classmethod
    def can_continue(cls, frontier: dict[str, Any]) -> bool:
        attempt_window_blocked = str(frontier.get("exhaustionReason") or "") in {
            "max_consecutive_failed_attempts",
            "max_provider_calls_per_root",
        }
        return bool(
            isinstance(frontier, dict)
            and cls.is_continuable_state(frontier.get("frontierState"))
            and int(frontier.get("remainingBudget") or 0) > 0
            and not attempt_window_blocked
        )

    @classmethod
    def no_progress(cls, frontier: dict[str, Any], *, reason: str = "no_progress") -> dict[str, Any]:
        """Terminal, zero-write frontier state when no next direction is provable."""

        current = copy.deepcopy(frontier)
        current.update(
            {
                "frontierState": "no_progress",
                "exhaustionReason": str(reason or "no_progress"),
                "progressCertificate": None,
                "updatedAt": cls._now(),
            }
        )
        return current

    def candidate_amap_budget(self, frontier: dict[str, Any]) -> AmapCallBudget:
        """Return the remaining candidate envelope for one planning-root execution.

        The initial execution may use the existing full Portfolio envelope.  A
        continuation receives only the root remainder and is additionally
        capped by ``maxAmapCallsPerContinuation``.  Persisted legacy frontiers
        without the new fields are treated as having consumed zero calls.
        """

        current = dict(frontier or {})
        limits = {**self.limits, **dict(current.get("limits") or {})}
        usage = self._normalized_amap_candidate_usage(current.get("amapCandidateUsage"))
        remaining = self._amap_candidate_remaining(usage, limits)
        continuation_round = max(0, int(current.get("continuationRound") or 0))
        terminal = continuation_round > 0 and not self.can_continue(current)
        reservations = current.get("amapCandidateReservations")
        reservation_in_flight = bool(
            isinstance(reservations, dict)
            and any(
                isinstance(item, dict) and str(item.get("state") or "") == "reserved"
                for item in reservations.values()
            )
        )
        total_external_max = int(remaining["remainingTotalExternal"])
        if continuation_round > 0:
            total_external_max = min(
                total_external_max,
                int(limits.get("maxAmapCallsPerContinuation") or 0),
            )
        if terminal or reservation_in_flight:
            total_external_max = 0
        return AmapCallBudget(
            place_text_max=(
                min(int(remaining["remainingPlaceTextAndDetail"]), total_external_max)
                if not terminal
                else 0
            ),
            place_around_max=(
                min(int(remaining["remainingPlaceAround"]), total_external_max)
                if not terminal
                else 0
            ),
            route_refresh_max=0,
            total_external_max=total_external_max,
            source="creative_portfolio_candidate_grounding",
        )

    def reserve_candidate_amap_budget(
        self,
        frontier: dict[str, Any],
        *,
        execution_id: str,
    ) -> dict[str, Any]:
        """Claim one bounded continuation envelope before external provider work."""

        execution = str(execution_id or "").strip()
        if not _CANDIDATE_BUDGET_EXECUTION_ID.fullmatch(execution):
            raise ValueError("candidate_amap_budget_execution_id_invalid")
        current = copy.deepcopy(frontier)
        raw_reservations = current.get("amapCandidateReservations")
        reservations = copy.deepcopy(raw_reservations) if isinstance(raw_reservations, dict) else {}
        existing = reservations.get(execution)
        if isinstance(existing, dict):
            state = str(existing.get("state") or "")
            return {
                "frontier": current,
                "status": "REPLAY_SETTLED" if state == "settled" else "REPLAY_RESERVED",
                "providerCallAllowed": False,
                "allocation": self._empty_amap_candidate_allocation(),
            }
        if any(
            isinstance(item, dict) and str(item.get("state") or "") == "reserved"
            for item in reservations.values()
        ):
            return {
                "frontier": current,
                "status": "CLAIM_IN_FLIGHT",
                "providerCallAllowed": False,
                "allocation": self._empty_amap_candidate_allocation(),
            }
        if len(reservations) >= _MAX_CANDIDATE_BUDGET_RESERVATIONS:
            raise ValueError("candidate_amap_budget_reservation_capacity_exceeded")
        budget = self.candidate_amap_budget(current)
        allocation = {
            "placeTextMax": int(budget.place_text_max),
            "placeAroundMax": int(budget.place_around_max),
            "totalExternalMax": int(budget.total_external_max),
        }
        if allocation["totalExternalMax"] <= 0:
            return {
                "frontier": current,
                "status": "BUDGET_EXHAUSTED",
                "providerCallAllowed": False,
                "allocation": self._empty_amap_candidate_allocation(),
            }
        reservations[execution] = {
            "state": "reserved",
            "allocation": copy.deepcopy(allocation),
            "actualUsage": None,
            "reservedAt": self._now(),
        }
        current["amapCandidateReservations"] = reservations
        current["updatedAt"] = self._now()
        return {
            "frontier": current,
            "status": "CLAIMED",
            "providerCallAllowed": True,
            "allocation": allocation,
        }

    def settle_candidate_amap_budget(
        self,
        frontier: dict[str, Any],
        *,
        execution_id: str,
        actual_usage: dict[str, Any],
    ) -> dict[str, Any]:
        """Settle actual provider calls once and release unused reservation capacity."""

        execution = str(execution_id or "").strip()
        if not _CANDIDATE_BUDGET_EXECUTION_ID.fullmatch(execution):
            raise ValueError("candidate_amap_budget_execution_id_invalid")
        current = copy.deepcopy(frontier)
        reservations = current.get("amapCandidateReservations")
        if not isinstance(reservations, dict) or not isinstance(reservations.get(execution), dict):
            raise ValueError("candidate_amap_budget_reservation_missing")
        reservation = reservations[execution]
        normalized_actual = self._normalized_amap_candidate_usage(actual_usage)
        if str(reservation.get("state") or "") == "settled":
            if self._normalized_amap_candidate_usage(reservation.get("actualUsage")) != normalized_actual:
                raise ValueError("candidate_amap_budget_settlement_mismatch")
            return {
                "frontier": current,
                "status": "REPLAY_SETTLED",
                "providerCallAllowed": False,
            }
        if str(reservation.get("state") or "") != "reserved":
            raise ValueError("candidate_amap_budget_reservation_invalid")
        allocation = reservation.get("allocation") if isinstance(reservation.get("allocation"), dict) else {}
        if (
            int(normalized_actual["usedPlaceText"]) + int(normalized_actual["usedPlaceDetail"])
            > int(allocation.get("placeTextMax") or 0)
            or int(normalized_actual["usedPlaceAround"]) > int(allocation.get("placeAroundMax") or 0)
            or int(normalized_actual["usedTotalExternal"]) > int(allocation.get("totalExternalMax") or 0)
        ):
            raise ValueError("candidate_amap_budget_settlement_exceeds_reservation")
        usage = self._merge_amap_candidate_usage(current.get("amapCandidateUsage"), normalized_actual)
        limits = {**self.limits, **dict(current.get("limits") or {})}
        remaining = self._amap_candidate_remaining(usage, limits)
        reservation.update(
            {
                "state": "settled",
                "actualUsage": normalized_actual,
                "settledAt": self._now(),
            }
        )
        current["amapCandidateUsage"] = usage
        current["amapCandidateRemaining"] = remaining
        current["updatedAt"] = self._now()
        if int(remaining["remainingTotalExternal"]) == 0 or (
            int(remaining["remainingPlaceTextAndDetail"]) == 0
            and int(remaining["remainingPlaceAround"]) == 0
        ):
            current["frontierState"] = "budget_exhausted"
            current["exhaustionReason"] = "max_amap_candidate_calls_per_root"
        return {
            "frontier": current,
            "status": "SETTLED",
            "providerCallAllowed": False,
        }

    @staticmethod
    def _empty_amap_candidate_allocation() -> dict[str, int]:
        return {
            "placeTextMax": 0,
            "placeAroundMax": 0,
            "totalExternalMax": 0,
        }

    @staticmethod
    def _empty_amap_candidate_usage() -> dict[str, int]:
        return {
            "usedPlaceText": 0,
            "usedPlaceDetail": 0,
            "usedPlaceAround": 0,
            "usedTotalExternal": 0,
        }

    @classmethod
    def _normalized_amap_candidate_usage(cls, value: Any) -> dict[str, int]:
        payload = value if isinstance(value, dict) else {}
        if isinstance(payload.get("used"), dict):
            payload = payload["used"]
        usage = {
            "usedPlaceText": max(0, int(payload.get("usedPlaceText") or 0)),
            "usedPlaceDetail": max(0, int(payload.get("usedPlaceDetail") or 0)),
            "usedPlaceAround": max(0, int(payload.get("usedPlaceAround") or 0)),
        }
        usage["usedTotalExternal"] = sum(usage.values())
        return usage

    @classmethod
    def _merge_amap_candidate_usage(cls, current: Any, delta: Any) -> dict[str, int]:
        existing = cls._normalized_amap_candidate_usage(current)
        increment = cls._normalized_amap_candidate_usage(delta)
        merged = {
            key: int(existing[key]) + int(increment[key])
            for key in ("usedPlaceText", "usedPlaceDetail", "usedPlaceAround")
        }
        merged["usedTotalExternal"] = sum(merged.values())
        return merged

    @classmethod
    def _amap_candidate_remaining(
        cls,
        usage: Any,
        limits: dict[str, Any],
    ) -> dict[str, int]:
        normalized = cls._normalized_amap_candidate_usage(usage)
        return {
            "remainingPlaceTextAndDetail": max(
                0,
                int(limits.get("maxAmapCandidateTextCallsPerRoot") or 0)
                - int(normalized["usedPlaceText"])
                - int(normalized["usedPlaceDetail"]),
            ),
            "remainingPlaceAround": max(
                0,
                int(limits.get("maxAmapCandidateAroundCallsPerRoot") or 0)
                - int(normalized["usedPlaceAround"]),
            ),
            "remainingTotalExternal": max(
                0,
                int(limits.get("maxAmapCandidateCallsPerRoot") or 0)
                - int(normalized["usedTotalExternal"]),
            ),
        }

    @classmethod
    def night_view_semantic_fingerprint(
        cls,
        *,
        goal: Any,
        city: Any,
        adcode: Any,
        experienceSpec: Any,
        briefPoolSlotDayTime: Any,
        routeContract: Any,
        candidateHints: Any,
    ) -> str:
        """Hash only normalized night-query semantics, never audit identities.

        Tenant/user/root identifiers select an isolated ledger entry below, but
        must not decide which night-view query comes first.  Candidate hints are
        an unordered semantic set; every other list remains structurally ordered
        because it is part of the compiled ExperienceSpec or route contract.
        """

        scope = cls._normalized_night_semantic_scope(briefPoolSlotDayTime)
        hints = sorted(
            {
                cls._normalized_night_text(value)
                for value in candidateHints or []
                if cls._normalized_night_text(value)
            }
        )
        material = {
            "goal": cls._night_allowlisted_mapping(
                goal,
                allowed_fields=_NIGHT_SEMANTIC_GOAL_FIELDS,
                invalid_code="night_view_semantic_goal_invalid",
            ),
            "city": cls._normalized_night_text(city),
            "adcode": cls._normalized_night_text(adcode),
            "experienceSpec": cls._night_experience_projection(experienceSpec),
            "briefPoolSlotDayTime": scope,
            "routeContract": cls._night_route_contract_projection(routeContract),
            "candidateHints": hints,
        }
        return cls._night_hash(material)

    @classmethod
    def night_view_query_fingerprint(cls, query: dict[str, Any]) -> str:
        """Return the canonical, non-secret identity of one frozen query.

        ``ItineraryService`` uses this public projection to select a persisted
        claim.  Keeping the normalization here makes the ledger's claim key and
        the Provider-bound request descriptor one contract, rather than having
        callers depend on private hashing helpers.
        """

        if not isinstance(query, dict):
            raise ValueError("night_view_query_invalid")
        return cls._night_hash(cls._normalized_night_value(query))

    @classmethod
    def claim_night_view_query(
        cls,
        frontier: dict[str, Any],
        *,
        scope: dict[str, Any],
        semantic_fingerprint: str,
        queries: list[dict[str, Any]],
        attempt_identity: str,
    ) -> dict[str, Any]:
        """Claim one unexecuted night query without advancing its cursor.

        The returned frontier is the sole durable representation and is designed
        to live under the existing ``creativeExplorationFrontier`` summary.  A
        caller must persist it by the existing compare-and-swap path before any
        Provider work, then call ``complete_night_view_query`` with a safe
        receipt only after a real query completed.
        """

        current = copy.deepcopy(frontier)
        normalized_scope = cls._normalized_night_scope(scope)
        fingerprint = cls._require_night_fingerprint(semantic_fingerprint)
        normalized_queries = cls._normalized_night_queries(queries)
        attempt = cls._require_night_attempt_identity(attempt_identity)
        progress = cls._night_progress(current)
        entry_key = cls._night_hash(
            {
                "scope": normalized_scope,
                "semanticFingerprint": fingerprint,
            }
        )
        entries = progress["entries"]
        entry = entries.get(entry_key)
        query_fingerprints = [item["queryFingerprint"] for item in normalized_queries]
        if entry is None:
            if len(entries) >= _MAX_NIGHT_VIEW_PROGRESS_SCOPES:
                raise ValueError("night_view_progress_scope_capacity_exceeded")
            entry = {
                "scope": normalized_scope,
                "scopeFingerprint": cls._night_hash(normalized_scope),
                "semanticFingerprint": fingerprint,
                "queryFingerprints": query_fingerprints,
                "queryCursor": 0,
                "executedQueryFingerprints": [],
                "attempts": {},
                "inFlightAttemptIdentity": None,
            }
            entries[entry_key] = entry
        else:
            cls._validate_night_entry(
                entry,
                scope=normalized_scope,
                semantic_fingerprint=fingerprint,
                query_fingerprints=query_fingerprints,
            )

        attempts = entry["attempts"]
        prior_attempt = attempts.get(attempt)
        if isinstance(prior_attempt, dict):
            query_fingerprint = str(prior_attempt.get("queryFingerprint") or "")
            selected = next(
                (item for item in normalized_queries if item["queryFingerprint"] == query_fingerprint),
                None,
            )
            if selected is None:
                raise ValueError("night_view_progress_attempt_query_missing")
            return cls._night_claim_result(
                current,
                entry,
                status="REPLAY",
                selected=selected,
                attempt_identity=attempt,
                provider_call_allowed=False,
            )

        in_flight = str(entry.get("inFlightAttemptIdentity") or "")
        if in_flight:
            return cls._night_claim_result(
                current,
                entry,
                status="CLAIM_IN_FLIGHT",
                selected=None,
                attempt_identity=attempt,
                provider_call_allowed=False,
            )

        executed = set(entry["executedQueryFingerprints"])
        selected = next(
            (item for item in normalized_queries if item["queryFingerprint"] not in executed),
            None,
        )
        if selected is None:
            return cls._night_claim_result(
                current,
                entry,
                status="NO_PROGRESS",
                selected=None,
                attempt_identity=attempt,
                provider_call_allowed=False,
            )

        if len(attempts) >= _MAX_NIGHT_VIEW_ATTEMPTS:
            raise ValueError("night_view_progress_attempt_capacity_exceeded")
        attempts[attempt] = {
            "queryFingerprint": selected["queryFingerprint"],
            "state": "claimed",
        }
        entry["inFlightAttemptIdentity"] = attempt
        return cls._night_claim_result(
            current,
            entry,
            status="CLAIMED",
            selected=selected,
            attempt_identity=attempt,
            provider_call_allowed=True,
        )

    @classmethod
    def complete_night_view_query(
        cls,
        frontier: dict[str, Any],
        *,
        scope: dict[str, Any],
        semantic_fingerprint: str,
        queries: list[dict[str, Any]],
        attempt_identity: str,
        provider_completed: bool,
        provider_receipt_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        """Resolve the exact claim; only a completed Provider query advances."""

        current = copy.deepcopy(frontier)
        normalized_scope = cls._normalized_night_scope(scope)
        fingerprint = cls._require_night_fingerprint(semantic_fingerprint)
        normalized_queries = cls._normalized_night_queries(queries)
        attempt = cls._require_night_attempt_identity(attempt_identity)
        progress = cls._night_progress(current)
        entry_key = cls._night_hash(
            {"scope": normalized_scope, "semanticFingerprint": fingerprint}
        )
        entry = progress["entries"].get(entry_key)
        if not isinstance(entry, dict):
            raise ValueError("night_view_progress_claim_missing")
        cls._validate_night_entry(
            entry,
            scope=normalized_scope,
            semantic_fingerprint=fingerprint,
            query_fingerprints=[item["queryFingerprint"] for item in normalized_queries],
        )
        attempt_state = entry["attempts"].get(attempt)
        if (
            not isinstance(attempt_state, dict)
            or str(entry.get("inFlightAttemptIdentity") or "") != attempt
            or str(attempt_state.get("state") or "") != "claimed"
        ):
            raise ValueError("night_view_progress_claim_not_active")
        query_fingerprint = str(attempt_state.get("queryFingerprint") or "")
        if query_fingerprint not in entry["queryFingerprints"]:
            raise ValueError("night_view_progress_claim_query_missing")

        entry["inFlightAttemptIdentity"] = None
        attempt_state["state"] = "completed" if provider_completed else "failed_before_progress"
        if provider_completed:
            receipt_fingerprint = cls._require_night_fingerprint(provider_receipt_fingerprint)
            attempt_state["receiptFingerprint"] = receipt_fingerprint
            if query_fingerprint not in entry["executedQueryFingerprints"]:
                entry["executedQueryFingerprints"].append(query_fingerprint)
            entry["queryCursor"] = len(entry["executedQueryFingerprints"])
        return {
            "frontier": current,
            "status": "COMPLETED" if provider_completed else "FAILED_NO_PROGRESS",
            "queryCursor": entry["queryCursor"],
            "executedQueryFingerprints": list(entry["executedQueryFingerprints"]),
            "attemptIdentity": attempt,
            "providerCallAllowed": False,
        }

    @classmethod
    def _night_progress(cls, frontier: dict[str, Any]) -> dict[str, Any]:
        existing = frontier.get("nightViewQueryProgress")
        if existing is None:
            progress = {"schemaVersion": _NIGHT_VIEW_PROGRESS_SCHEMA, "entries": {}}
            frontier["nightViewQueryProgress"] = progress
            return progress
        if (
            not isinstance(existing, dict)
            or existing.get("schemaVersion") != _NIGHT_VIEW_PROGRESS_SCHEMA
            or not isinstance(existing.get("entries"), dict)
        ):
            raise ValueError("night_view_progress_ledger_invalid")
        return existing

    @classmethod
    def _night_claim_result(
        cls,
        frontier: dict[str, Any],
        entry: dict[str, Any],
        *,
        status: str,
        selected: dict[str, Any] | None,
        attempt_identity: str,
        provider_call_allowed: bool,
    ) -> dict[str, Any]:
        result = {
            "frontier": frontier,
            "status": status,
            "queryCursor": int(entry["queryCursor"]),
            "executedQueryFingerprints": list(entry["executedQueryFingerprints"]),
            "attemptIdentity": attempt_identity,
            "providerCallAllowed": provider_call_allowed,
        }
        if selected is not None:
            result["query"] = copy.deepcopy(selected["query"])
            result["queryFingerprint"] = selected["queryFingerprint"]
        return result

    @classmethod
    def _validate_night_entry(
        cls,
        entry: dict[str, Any],
        *,
        scope: dict[str, Any],
        semantic_fingerprint: str,
        query_fingerprints: list[str],
    ) -> None:
        required = {
            "scope",
            "scopeFingerprint",
            "semanticFingerprint",
            "queryFingerprints",
            "queryCursor",
            "executedQueryFingerprints",
            "attempts",
            "inFlightAttemptIdentity",
        }
        if set(entry) != required:
            raise ValueError("night_view_progress_ledger_invalid")
        if (
            entry["scope"] != scope
            or entry["scopeFingerprint"] != cls._night_hash(scope)
            or entry["semanticFingerprint"] != semantic_fingerprint
            or entry["queryFingerprints"] != query_fingerprints
            or not isinstance(entry["executedQueryFingerprints"], list)
            or not isinstance(entry["attempts"], dict)
        ):
            raise ValueError("night_view_progress_scope_mismatch")
        executed = entry["executedQueryFingerprints"]
        if (
            len(executed) != len(set(executed))
            or any(item not in query_fingerprints for item in executed)
            or entry["queryCursor"] != len(executed)
            or not 0 <= entry["queryCursor"] <= len(query_fingerprints)
        ):
            raise ValueError("night_view_progress_ledger_invalid")

    @classmethod
    def _normalized_night_queries(cls, queries: Any) -> list[dict[str, Any]]:
        if not isinstance(queries, list) or not queries or len(queries) > _MAX_NIGHT_VIEW_QUERIES:
            raise ValueError("night_view_progress_query_set_invalid")
        normalized = []
        seen = set()
        for query in queries:
            if not isinstance(query, dict):
                raise ValueError("night_view_progress_query_set_invalid")
            material = cls._normalized_night_value(query)
            if not isinstance(material, dict) or not material:
                raise ValueError("night_view_progress_query_set_invalid")
            fingerprint = cls._night_hash(material)
            if fingerprint in seen:
                raise ValueError("night_view_progress_duplicate_query")
            seen.add(fingerprint)
            normalized.append({"query": material, "queryFingerprint": fingerprint})
        return normalized

    @classmethod
    def _normalized_night_scope(cls, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict) or set(value) != set(_NIGHT_SCOPE_FIELDS):
            raise ValueError("night_view_progress_scope_invalid")
        result: dict[str, Any] = {}
        for key in _NIGHT_SCOPE_FIELDS:
            item = value.get(key)
            if key == "dayNumber":
                if type(item) is not int or item < 1:
                    raise ValueError("night_view_progress_scope_invalid")
                result[key] = item
            else:
                normalized = cls._normalized_night_text(item)
                if not normalized:
                    raise ValueError("night_view_progress_scope_invalid")
                result[key] = normalized
        return result

    @classmethod
    def _normalized_night_semantic_scope(cls, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict) or set(value) != set(_NIGHT_SEMANTIC_SCOPE_FIELDS):
            raise ValueError("night_view_semantic_scope_invalid")
        result: dict[str, Any] = {}
        for key in _NIGHT_SEMANTIC_SCOPE_FIELDS:
            item = value.get(key)
            if key == "dayNumber":
                if type(item) is not int or item < 1:
                    raise ValueError("night_view_semantic_scope_invalid")
                result[key] = item
            else:
                normalized = cls._normalized_night_text(item)
                if not normalized:
                    raise ValueError("night_view_semantic_scope_invalid")
                result[key] = normalized
        return result

    @classmethod
    def _night_experience_projection(cls, value: Any) -> dict[str, Any]:
        projected = cls._night_allowlisted_mapping(
            value,
            allowed_fields=_NIGHT_SEMANTIC_EXPERIENCE_FIELDS,
            invalid_code="night_view_semantic_experience_invalid",
        )
        for field in ("semanticFacets", "desiredSignals", "avoidSignals"):
            raw_values = projected.get(field, [])
            if not isinstance(raw_values, list):
                raise ValueError("night_view_semantic_experience_invalid")
            projected[field] = sorted(set(raw_values))
        for field in ("evidencePolicy", "groundingPolicy"):
            raw_policy = projected.get(field, {})
            if not isinstance(raw_policy, dict):
                raise ValueError("night_view_semantic_experience_invalid")
        return projected

    @classmethod
    def _night_route_contract_projection(cls, value: Any) -> dict[str, str]:
        if not isinstance(value, dict):
            raise ValueError("night_view_semantic_route_contract_invalid")
        sanitized = cls._night_drop_audit_fields(value)
        if set(sanitized) != {"fingerprint"}:
            raise ValueError("night_view_semantic_route_contract_invalid")
        fingerprint = cls._require_night_fingerprint(sanitized.get("fingerprint"))
        return {"fingerprint": fingerprint}

    @classmethod
    def _night_allowlisted_mapping(
        cls,
        value: Any,
        *,
        allowed_fields: frozenset[str],
        invalid_code: str,
    ) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError(invalid_code)
        sanitized = cls._night_drop_audit_fields(value)
        if set(sanitized) - allowed_fields:
            raise ValueError(invalid_code)
        return {
            field: cls._night_semantic_policy_value(sanitized[field])
            for field in sorted(sanitized)
        }

    @classmethod
    def _night_semantic_policy_value(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                cls._normalized_night_key(key): cls._night_semantic_policy_value(item)
                for key, item in sorted(cls._night_drop_audit_fields(value).items(), key=lambda item: str(item[0]))
            }
        if isinstance(value, (list, tuple)):
            return [cls._night_semantic_policy_value(item) for item in value]
        return cls._normalized_night_value(value)

    @classmethod
    def _night_drop_audit_fields(cls, value: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = cls._normalized_night_key(key)
            audit_alias = re.sub(r"[^a-z0-9]", "", normalized_key.casefold())
            if audit_alias in _NIGHT_NON_SEMANTIC_FIELD_ALIASES:
                continue
            result[normalized_key] = item
        return result

    @classmethod
    def _normalized_night_value(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                cls._normalized_night_key(key): cls._normalized_night_value(item)
                for key, item in sorted(value.items(), key=lambda item: cls._normalized_night_key(item[0]))
                if cls._normalized_night_key(key)
            }
        if isinstance(value, (list, tuple)):
            return [cls._normalized_night_value(item) for item in value]
        if isinstance(value, str):
            return cls._normalized_night_text(value)
        if type(value) in {int, float, bool} or value is None:
            return value
        raise ValueError("night_view_semantic_value_invalid")

    @staticmethod
    def _normalized_night_text(value: Any) -> str:
        return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value or "")).casefold())

    @staticmethod
    def _normalized_night_key(value: Any) -> str:
        """Keep contract field names stable while canonicalizing only their form."""

        return unicodedata.normalize("NFKC", str(value or "")).strip()

    @staticmethod
    def _night_hash(value: Any) -> str:
        material = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return sha256(material.encode("utf-8")).hexdigest()

    @staticmethod
    def _require_night_fingerprint(value: Any) -> str:
        fingerprint = str(value or "").strip().casefold()
        if _HEX_64.fullmatch(fingerprint) is None:
            raise ValueError("night_view_progress_fingerprint_invalid")
        return fingerprint

    @staticmethod
    def _require_night_attempt_identity(value: Any) -> str:
        attempt = str(value or "").strip()
        if _NIGHT_ATTEMPT_ID.fullmatch(attempt) is None:
            raise ValueError("night_view_progress_attempt_identity_invalid")
        return attempt

    @staticmethod
    def _unique(values: Any) -> list[str]:
        return list(dict.fromkeys(str(item) for item in values if str(item)))

    @classmethod
    def _merge(cls, prior: Any, added: Any) -> list[str]:
        return cls._unique([*(prior or []), *(added or [])])

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

"""Server-owned, persistent candidate frontier for Simple Direction.

The client never supplies an entity cursor, query page, school name, or AMap
identity.  This service operates on JSON material that is frozen in a
portfolio summary and returns new copies so callers can persist it atomically
with the corresponding proposal/choice execution.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any, Iterable

from src.services.entity_qualification_evidence_service import EntityQualificationEvidenceService


class SimpleDirectionFrontierService:
    SCHEMA_VERSION = "simple-direction-frontier-v1"
    ATTEMPT_SCHEMA_VERSION = "simple-direction-frontier-attempt-v1"
    EXPLORATION_ORDERING_VERSION = "simple-direction-exploration-ordering-v1"
    DEFAULT_OFFSET = 5
    _AMAP_ID_RE = re.compile(r"B[0-9A-Z]{8,31}")
    _PROVIDER_OUTCOMES = frozenset({"success", "rejected", "failure"})
    _CENTER_ROLES = frozenset({"", "midpoint", "predecessor", "successor"})
    _BLOCKING_LAYERS = frozenset({"", "qualification", "poi", "route", "provider"})
    _DISPOSITIONS = frozenset({"used_ready", "assigned_partial", "novelty_collision", "provider_pending"})

    @classmethod
    def create(
        cls,
        *,
        planning_root_id: str,
        request_contract_fingerprint: str,
        evidence: dict[str, Any],
        locality: str,
        max_pages_per_query: int,
        exploration_seed: str | None = None,
    ) -> dict[str, Any]:
        evidence_fingerprint = str(evidence.get("contentSha256") or "") or cls._fingerprint(evidence)
        locality_key = cls._normalize_locality(locality)
        entities: list[dict[str, Any]] = []
        for item in evidence.get("entities") or []:
            if not isinstance(item, dict):
                continue
            canonical_name = str(item.get("canonicalName") or "").strip()
            if not canonical_name or cls._normalize_locality(item.get("locality")) != locality_key:
                continue
            entity_fingerprint = EntityQualificationEvidenceService.entity_fingerprint(
                evidence_fingerprint=evidence_fingerprint,
                entity=item,
            )
            qualification_binding = EntityQualificationEvidenceService.build_binding(
                evidence=evidence,
                entity=item,
                planning_root_id=planning_root_id,
                request_contract_fingerprint=request_contract_fingerprint,
            )
            entities.append(
                {
                    "evidenceEntityFingerprint": entity_fingerprint,
                    "canonicalName": canonical_name,
                    "locality": str(item.get("locality") or "").strip(),
                    "qualificationBinding": qualification_binding,
                    "state": "untried",
                    "attemptedQueryFingerprints": [],
                    "attemptedPages": [],
                }
            )
        maximum_pages = max(1, min(int(max_pages_per_query), 10))
        frontier = {
            "schemaVersion": cls.SCHEMA_VERSION,
            "planningRootId": str(planning_root_id or ""),
            "requestContractFingerprint": str(request_contract_fingerprint or ""),
            "qualificationEvidenceFingerprint": evidence_fingerprint,
            "qualificationScheme": str(evidence.get("qualificationScheme") or ""),
            "qualificationValue": str(evidence.get("qualificationValue") or ""),
            "qualifiedEntityFrontier": entities,
            "slotFrontiers": {},
            "remainingQueryScopes": [],
            "executionProfile": {
                "maxPagesPerQuery": maximum_pages,
                "pageOffset": cls.DEFAULT_OFFSET,
                "profileFingerprint": cls._fingerprint(
                    {"maxPagesPerQuery": maximum_pages, "pageOffset": cls.DEFAULT_OFFSET}
                ),
            },
            "remainingQualifiedEntityCount": len(entities),
            "remainingPoiPageCount": 0,
            "frontierStatus": "has_more" if entities else "qualification_exhausted",
        }
        if exploration_seed is not None:
            frontier["explorationOrdering"] = cls._exploration_ordering(
                seed=exploration_seed,
                entity_fingerprints=[str(item["evidenceEntityFingerprint"]) for item in entities],
            )
        frontier["frontierFingerprint"] = cls._frontier_fingerprint(frontier)
        return frontier

    @classmethod
    def begin_attempt(
        cls,
        frontier: dict[str, Any],
        *,
        campus_slots: Iterable[dict[str, Any]],
        excluded_entity_fingerprints: Iterable[str] = (),
    ) -> dict[str, Any]:
        cls._validate(frontier)
        slots = [
            {"dayNumber": int(item.get("dayNumber") or 0), "slotId": str(item.get("slotId") or "")}
            for item in campus_slots
            if isinstance(item, dict) and str(item.get("slotId") or "")
        ]
        slots.sort(key=lambda item: (item["dayNumber"], item["slotId"]))
        entities = [item for item in frontier.get("qualifiedEntityFrontier") or [] if isinstance(item, dict)]
        ordering = frontier.get("explorationOrdering")
        if isinstance(ordering, dict):
            order = {value: rank for rank, value in enumerate(ordering["orderedEntityFingerprints"])}
            entities.sort(key=lambda item: order[item["evidenceEntityFingerprint"]])
        excluded = {str(item or "") for item in excluded_entity_fingerprints if str(item or "")}
        untried = [
            item
            for item in entities
            if str(item.get("state") or "") == "untried"
            and str(item.get("evidenceEntityFingerprint") or "") not in excluded
        ]
        selected = list(untried[: len(slots)])
        new_count = len(selected)
        retry_group_id = ""
        if not selected and slots:
            retry_group = cls._retryable_grounded_group(frontier, required_size=len(slots))
            if retry_group:
                selected = retry_group
                retry_group_id = str(retry_group[0].get("frontierAttemptGroupId") or "")
        if len(selected) < len(slots) and selected:
            reusable = [
                item
                for item in entities
                if str(item.get("state") or "") in {"used_ready", "assigned_partial"}
                and cls._canonical_amap_id(item.get("canonicalAmapId"))
                and item not in selected
            ]
            selected.extend(reusable[: len(slots) - len(selected)])
            if len(selected) < len(slots):
                # Do not split a grounded collision pair merely to complete a
                # one-new-entity fallback.  Retry that pair as a unit first;
                # otherwise one orphaned member could keep a false `has_more`
                # state that cannot satisfy the original campus cardinality.
                retry_group = cls._retryable_grounded_group(frontier, required_size=len(slots))
                if retry_group:
                    selected = retry_group
                    new_count = 0
                    retry_group_id = str(retry_group[0].get("frontierAttemptGroupId") or "")
        assignments: list[dict[str, Any]] = []
        profile = frontier.get("executionProfile") if isinstance(frontier.get("executionProfile"), dict) else {}
        maximum_pages = max(1, int(profile.get("maxPagesPerQuery") or 1))
        offset = max(1, min(int(profile.get("pageOffset") or cls.DEFAULT_OFFSET), 25))
        for slot, entity in zip(slots, selected):
            attempted_pages = [
                cls._positive_int(value) for value in entity.get("attemptedPages") or [] if cls._positive_int(value)
            ]
            assignment_mode = "new_entity"
            if str(entity.get("state") or "") == "grounded":
                assignment_mode = "collision_retry"
            elif str(entity.get("state") or "") == "assigned_partial" and retry_group_id:
                assignment_mode = "partial_retry"
            elif str(entity.get("state") or "") in {"used_ready", "assigned_partial"}:
                assignment_mode = "single_new_anchor_fallback_reuse"
            # A collision retry advances the replaceable slot frontiers, not the
            # already-grounded campus identity.  Keep its successful campus page
            # frozen so the executor can rebind the exact canonical AMap POI.
            page = (
                max(attempted_pages, default=1)
                if assignment_mode == "collision_retry"
                else min(max(attempted_pages, default=0) + 1, maximum_pages)
            )
            query_scope_fingerprint = cls._fingerprint(
                {
                    "planningRootId": str(frontier.get("planningRootId") or ""),
                    "requestContractFingerprint": str(frontier.get("requestContractFingerprint") or ""),
                    "evidenceEntityFingerprint": str(entity.get("evidenceEntityFingerprint") or ""),
                    "slotId": slot["slotId"],
                    "dayNumber": slot["dayNumber"],
                }
            )
            query_fingerprint = cls._fingerprint(
                {
                    "queryScopeFingerprint": query_scope_fingerprint,
                    "canonicalName": str(entity.get("canonicalName") or ""),
                    "qualificationBinding": copy.deepcopy(entity.get("qualificationBinding")),
                    "page": page,
                    "offset": offset,
                }
            )
            assignments.append(
                {
                    **slot,
                    "evidenceEntityFingerprint": str(entity.get("evidenceEntityFingerprint") or ""),
                    "canonicalName": str(entity.get("canonicalName") or ""),
                    "locality": str(entity.get("locality") or ""),
                    "qualificationBinding": copy.deepcopy(entity.get("qualificationBinding")),
                    "qualificationBindingFingerprint": str(
                        (entity.get("qualificationBinding") or {}).get("bindingFingerprint")
                    )
                    if isinstance(entity.get("qualificationBinding"), dict)
                    else "",
                    "isNewQualificationEntity": str(entity.get("state") or "") == "untried",
                    "assignmentMode": assignment_mode,
                    "frontierAttemptGroupId": str(entity.get("frontierAttemptGroupId") or "") or None,
                    "priorCanonicalAmapId": cls._canonical_amap_id(entity.get("canonicalAmapId")) or None,
                    "page": page,
                    "offset": offset,
                    "queryScopeFingerprint": query_scope_fingerprint,
                    "queryFingerprint": query_fingerprint,
                }
            )
        attempt = {
            "schemaVersion": cls.ATTEMPT_SCHEMA_VERSION,
            "frontierFingerprint": str(frontier.get("frontierFingerprint") or cls._frontier_fingerprint(frontier)),
            "campusAssignments": assignments,
            "twoNewAnchorsRequired": bool(len(slots) >= 2 and new_count >= 2),
            "singleNewAnchorFallback": bool(len(slots) >= 2 and new_count == 1 and len(assignments) == len(slots)),
            "newQualifiedEntityCount": new_count,
            "requestedCampusSlotCount": len(slots),
            "frontierAttemptGroupId": retry_group_id or None,
        }
        claimed_seed_ids = {
            cls._canonical_amap_id(item.get("canonicalAmapId"))
            for item in selected
            if cls._canonical_amap_id(item.get("canonicalAmapId"))
        }
        slot_queries = cls.claim_slot_queries(
            frontier,
            allowed_day_seed_amap_ids=claimed_seed_ids,
        )
        if slot_queries:
            attempt["slotQueries"] = slot_queries
            attempt["remainingQueryScopes"] = cls.remaining_query_scopes(
                frontier,
                allowed_day_seed_amap_ids=claimed_seed_ids,
            )
        attempt["attemptFingerprint"] = cls._fingerprint(attempt)
        return attempt

    @classmethod
    def reconcile_attempt(
        cls,
        frontier: dict[str, Any],
        *,
        attempt: dict[str, Any],
        outcomes: Iterable[dict[str, Any]],
        proposal_id: str | None,
        disposition: str,
        blocking_layer: str = "",
        reason_code: str = "",
        continuation_metadata: dict[str, Any] | None = None,
        remaining_query_scopes: Iterable[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        cls._validate(frontier)
        if str(attempt.get("frontierFingerprint") or "") != str(
            frontier.get("frontierFingerprint") or cls._frontier_fingerprint(frontier)
        ):
            raise ValueError("simple_direction_frontier_attempt_stale")
        normalized_outcomes = cls.validate_attempt_outcomes(attempt=attempt, outcomes=outcomes)
        normalized_disposition = str(disposition or "").strip().casefold()
        if normalized_disposition not in cls._DISPOSITIONS:
            raise ValueError("simple_direction_frontier_disposition_invalid")
        provider_outcomes = {str(item.get("providerOutcome") or "") for item in normalized_outcomes}
        if normalized_disposition in {"used_ready", "novelty_collision"} and provider_outcomes != {"success"}:
            raise ValueError("simple_direction_frontier_disposition_outcome_mismatch")
        if normalized_disposition != "provider_pending" and "failure" in provider_outcomes:
            raise ValueError("simple_direction_frontier_disposition_outcome_mismatch")
        normalized_blocking_layer = str(blocking_layer or "").strip().casefold()
        if normalized_blocking_layer not in cls._BLOCKING_LAYERS:
            raise ValueError("simple_direction_frontier_blocking_layer_invalid")
        result = copy.deepcopy(frontier)
        if remaining_query_scopes is not None:
            result["remainingQueryScopes"] = cls.normalize_remaining_query_scopes(remaining_query_scopes)
            result["remainingQueryScopesAuthoritative"] = True
        result["campusSlotCount"] = max(
            int(attempt.get("requestedCampusSlotCount") or 0),
            len([item for item in attempt.get("campusAssignments") or [] if isinstance(item, dict)]),
        )
        entity_by_fingerprint = {
            str(item.get("evidenceEntityFingerprint") or ""): item
            for item in result.get("qualifiedEntityFrontier") or []
            if isinstance(item, dict)
        }
        outcome_by_slot = {str(item.get("slotId") or ""): item for item in normalized_outcomes}
        attempt_group_id = str(attempt.get("attemptFingerprint") or "") or cls._fingerprint(
            {
                "frontierFingerprint": str(attempt.get("frontierFingerprint") or ""),
                "entities": sorted(
                    str(item.get("evidenceEntityFingerprint") or "")
                    for item in attempt.get("campusAssignments") or []
                    if isinstance(item, dict)
                ),
            }
        )
        for assignment in attempt.get("campusAssignments") or []:
            if not isinstance(assignment, dict):
                continue
            entity = entity_by_fingerprint.get(str(assignment.get("evidenceEntityFingerprint") or ""))
            if entity is None:
                raise ValueError("simple_direction_frontier_entity_missing")
            outcome = outcome_by_slot.get(str(assignment.get("slotId") or ""), {})
            provider_outcome = str(outcome.get("providerOutcome") or "")
            query_fingerprint = str(outcome.get("queryFingerprint") or "")
            page = cls._positive_int(outcome.get("page"))
            assignment_mode = str(assignment.get("assignmentMode") or "")
            mutates_entity_frontier = bool(
                assignment.get("isNewQualificationEntity") is True
                or assignment_mode == "collision_retry"
                or str(entity.get("state") or "") == "grounded"
            )
            if not mutates_entity_frontier:
                # A single-new-anchor fallback reuses an already assigned
                # entity.  Its historical state/proposal ownership is not
                # rewritten by the new direction.
                continue
            if provider_outcome == "failure" or normalized_disposition == "provider_pending":
                entity["state"] = "grounding_pending"
                entity["reasonCode"] = str(outcome.get("reasonCode") or "provider_unavailable")
                continue
            if query_fingerprint and query_fingerprint not in entity["attemptedQueryFingerprints"]:
                entity["attemptedQueryFingerprints"].append(query_fingerprint)
            if page and page not in entity["attemptedPages"]:
                entity["attemptedPages"].append(page)
            if provider_outcome == "rejected":
                maximum_pages = max(
                    1,
                    int((result.get("executionProfile") or {}).get("maxPagesPerQuery") or 1),
                )
                if page and page < maximum_pages:
                    # A successful query that yielded no admissible canonical
                    # campus exhausts only that page, not the frozen entity.
                    # The next server-side attempt will resume at page + 1.
                    entity["state"] = "untried"
                    entity["reasonCode"] = "poi_page_remaining"
                else:
                    entity["state"] = "rejected"
                    entity["reasonCode"] = str(outcome.get("reasonCode") or "grounding_rejected")
                continue
            selected_amap_id = cls._canonical_amap_id(outcome.get("selectedAmapId"))
            entity["canonicalAmapId"] = selected_amap_id
            if normalized_disposition == "used_ready":
                entity["state"] = "used_ready"
                entity["assignedProposalId"] = str(proposal_id or "") or None
                entity.pop("reasonCode", None)
            elif normalized_disposition == "assigned_partial":
                entity["state"] = "assigned_partial"
                entity["assignedProposalId"] = str(proposal_id or "") or None
                entity["frontierAttemptGroupId"] = attempt_group_id
                entity["assignedDayNumber"] = int(assignment.get("dayNumber") or 0)
                entity["assignedSlotId"] = str(assignment.get("slotId") or "")
                if isinstance(continuation_metadata, dict):
                    entity["continuationMetadata"] = copy.deepcopy(continuation_metadata)
                entity.pop("reasonCode", None)
            elif normalized_disposition == "novelty_collision":
                entity["state"] = "grounded"
                entity["frontierAttemptGroupId"] = attempt_group_id
                entity["assignedDayNumber"] = int(assignment.get("dayNumber") or 0)
                entity["assignedSlotId"] = str(assignment.get("slotId") or "")
                if isinstance(continuation_metadata, dict):
                    entity["continuationMetadata"] = copy.deepcopy(continuation_metadata)
                entity["reasonCode"] = "novelty_collision"
            else:
                entity["state"] = "rejected"
                entity["reasonCode"] = str(outcome.get("reasonCode") or normalized_disposition or "grounding_rejected")
        if normalized_disposition == "used_ready":
            result.pop("terminalBlockingLayer", None)
            result.pop("terminalReasonCode", None)
        elif normalized_blocking_layer:
            result["terminalBlockingLayer"] = normalized_blocking_layer
            result["terminalReasonCode"] = str(reason_code or normalized_disposition or "") or None
        elif normalized_disposition == "novelty_collision":
            result["terminalBlockingLayer"] = "poi"
            result["terminalReasonCode"] = str(reason_code or "novelty_collision")
        elif normalized_disposition == "assigned_partial":
            result["terminalBlockingLayer"] = "poi"
            result["terminalReasonCode"] = str(reason_code or "proposal_incomplete")
        cls._refresh_status(result)
        return result

    @classmethod
    def terminalize_attempt(
        cls,
        frontier: dict[str, Any],
        *,
        attempt: dict[str, Any],
        reason_code: str,
    ) -> dict[str, Any]:
        """Consume one abandoned claim without inventing Provider success.

        Once execution has crossed the persistent claim boundary, replaying the
        same query page would spend the root's bounded frontier twice.  A local
        execution failure therefore rejects the claimed page fail-closed.  The
        explicit ``providerCalled=False`` lineage distinguishes this terminal
        bookkeeping from real Provider evidence.
        """

        normalized_reason = str(reason_code or "simple_direction_execution_failed_after_claim").strip()
        outcomes = [
            {
                "slotId": str(assignment.get("slotId") or ""),
                "evidenceEntityFingerprint": str(assignment.get("evidenceEntityFingerprint") or ""),
                "providerOutcome": "rejected",
                "providerCalled": False,
                "outcomeSource": "server_fail_closed_terminalization",
                "selectedAmapId": None,
                "queryFingerprint": str(assignment.get("queryFingerprint") or ""),
                "page": cls._positive_int(assignment.get("page")),
                "reasonCode": normalized_reason,
                "rejectionReasonCodes": [normalized_reason],
            }
            for assignment in attempt.get("campusAssignments") or []
            if isinstance(assignment, dict)
        ]
        normalized_outcomes = cls.validate_attempt_outcomes(attempt=attempt, outcomes=outcomes)
        result = cls.reconcile_attempt(
            frontier,
            attempt=attempt,
            outcomes=normalized_outcomes,
            proposal_id=None,
            disposition="assigned_partial",
            blocking_layer="qualification",
            reason_code=normalized_reason,
        )
        for raw_query in attempt.get("slotQueries") or []:
            if not isinstance(raw_query, dict):
                continue
            expected_query = cls.begin_slot_query(
                result,
                day_number=int(raw_query.get("dayNumber") or 0),
                slot_id=str(raw_query.get("slotId") or ""),
                day_seed_amap_id=str(raw_query.get("daySeedAmapId") or ""),
                query_scope_fingerprint=str(raw_query.get("queryScopeFingerprint") or ""),
            )
            for field in (
                "slotFrontierKey",
                "queryFingerprint",
                "page",
                "offset",
                "requestContractFingerprint",
            ):
                if str(raw_query.get(field) or "") != str(expected_query.get(field) or ""):
                    raise ValueError("simple_direction_slot_frontier_query_identity_mismatch")
            result = cls.record_slot_query(
                result,
                query=raw_query,
                provider_outcome="rejected",
                admitted_physical_groups=[],
                rejected_physical_groups=[],
            )
            stored_query = (result.get("slotFrontiers") or {}).get(str(raw_query.get("slotFrontierKey") or ""))
            if isinstance(stored_query, dict):
                stored_query["lastTerminalizationReasonCode"] = normalized_reason
        cls._refresh_status(result)
        return {"frontier": result, "outcomes": normalized_outcomes}

    @classmethod
    def validate_attempt_outcomes(
        cls,
        *,
        attempt: dict[str, Any],
        outcomes: Iterable[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Validate the complete Provider result before any cursor mutation."""

        assignments = [item for item in attempt.get("campusAssignments") or [] if isinstance(item, dict)]
        assignment_by_slot: dict[str, dict[str, Any]] = {}
        for assignment in assignments:
            slot_id = str(assignment.get("slotId") or "")
            if not slot_id or slot_id in assignment_by_slot:
                raise ValueError("simple_direction_frontier_attempt_assignment_invalid")
            assignment_by_slot[slot_id] = assignment
        supplied_outcomes = list(outcomes)
        if any(not isinstance(item, dict) for item in supplied_outcomes):
            raise ValueError("simple_direction_frontier_outcomes_invalid")
        normalized = [copy.deepcopy(item) for item in supplied_outcomes]
        outcome_by_slot: dict[str, dict[str, Any]] = {}
        for outcome in normalized:
            slot_id = str(outcome.get("slotId") or "")
            if not slot_id or slot_id in outcome_by_slot:
                raise ValueError("simple_direction_frontier_outcomes_duplicate")
            outcome_by_slot[slot_id] = outcome
        if set(assignment_by_slot) != set(outcome_by_slot):
            raise ValueError("simple_direction_frontier_outcomes_incomplete")
        for slot_id, assignment in assignment_by_slot.items():
            outcome = outcome_by_slot[slot_id]
            expected_entity = str(assignment.get("evidenceEntityFingerprint") or "")
            if str(outcome.get("evidenceEntityFingerprint") or "") != expected_entity:
                raise ValueError("simple_direction_frontier_outcome_identity_mismatch")
            expected_query_fingerprint = str(assignment.get("queryFingerprint") or "")
            if str(outcome.get("queryFingerprint") or "") != expected_query_fingerprint:
                raise ValueError("simple_direction_frontier_outcome_query_identity_mismatch")
            expected_page = cls._positive_int(assignment.get("page"))
            if cls._positive_int(outcome.get("page")) != expected_page:
                raise ValueError("simple_direction_frontier_outcome_page_identity_mismatch")
            provider_outcome = str(outcome.get("providerOutcome") or "").strip().casefold()
            if provider_outcome not in cls._PROVIDER_OUTCOMES:
                raise ValueError("simple_direction_frontier_provider_outcome_invalid")
            outcome["providerOutcome"] = provider_outcome
            if provider_outcome == "success":
                selected_amap_id = cls._canonical_amap_id(outcome.get("selectedAmapId"))
                if not selected_amap_id:
                    raise ValueError("simple_direction_frontier_success_identity_missing")
                prior_amap_id = cls._canonical_amap_id(assignment.get("priorCanonicalAmapId"))
                if prior_amap_id and selected_amap_id != prior_amap_id:
                    raise ValueError("simple_direction_frontier_reused_identity_mismatch")
                outcome["selectedAmapId"] = selected_amap_id
        return normalized

    @classmethod
    def begin_slot_query(
        cls,
        frontier: dict[str, Any],
        *,
        day_number: int,
        slot_id: str,
        day_seed_amap_id: str,
        query_scope_fingerprint: str,
    ) -> dict[str, Any]:
        cls._validate(frontier)
        key_material = {
            "dayNumber": int(day_number),
            "slotId": str(slot_id),
            "daySeedAmapId": str(day_seed_amap_id or "").strip().upper(),
            "queryScopeFingerprint": str(query_scope_fingerprint or ""),
            "requestContractFingerprint": str(frontier.get("requestContractFingerprint") or ""),
        }
        key = cls._fingerprint(key_material)
        stored = (frontier.get("slotFrontiers") or {}).get(key)
        next_page = int(stored.get("nextPage") or 1) if isinstance(stored, dict) else 1
        profile = frontier.get("executionProfile") if isinstance(frontier.get("executionProfile"), dict) else {}
        maximum_pages = max(1, int(profile.get("maxPagesPerQuery") or 1))
        page = max(next_page, 1)
        query = {
            **key_material,
            "slotFrontierKey": key,
            "page": page,
            "offset": max(1, min(int(profile.get("pageOffset") or cls.DEFAULT_OFFSET), 25)),
            "exhausted": page > maximum_pages,
        }
        query["queryFingerprint"] = cls._fingerprint(query)
        return query

    @classmethod
    def record_slot_query(
        cls,
        frontier: dict[str, Any],
        *,
        query: dict[str, Any],
        provider_outcome: str,
        admitted_physical_groups: Iterable[str],
        rejected_physical_groups: Iterable[dict[str, Any]],
    ) -> dict[str, Any]:
        cls._validate(frontier)
        result = copy.deepcopy(frontier)
        key = str(query.get("slotFrontierKey") or "")
        if not key:
            raise ValueError("simple_direction_slot_frontier_identity_missing")
        slot_frontiers = result.setdefault("slotFrontiers", {})
        current = slot_frontiers.get(key)
        if not isinstance(current, dict):
            current = {
                "dayNumber": int(query.get("dayNumber") or 0),
                "slotId": str(query.get("slotId") or ""),
                "daySeedAmapId": str(query.get("daySeedAmapId") or ""),
                "queryScopeFingerprint": str(query.get("queryScopeFingerprint") or ""),
                "nextPage": 1,
                "attemptedQueryFingerprints": [],
                "attemptedPages": [],
                "admittedPhysicalGroups": [],
                "rejectedPhysicalGroups": [],
            }
            slot_frontiers[key] = current
        current.update(cls._query_scope_metadata(query))
        normalized_provider_outcome = str(provider_outcome or "").strip().casefold()
        if normalized_provider_outcome not in cls._PROVIDER_OUTCOMES:
            raise ValueError("simple_direction_slot_frontier_provider_outcome_invalid")
        query_fingerprint = str(query.get("queryFingerprint") or "")
        if normalized_provider_outcome in {"success", "rejected"}:
            if query_fingerprint and query_fingerprint not in current["attemptedQueryFingerprints"]:
                current["attemptedQueryFingerprints"].append(query_fingerprint)
            page = max(1, int(query.get("page") or 1))
            if page not in current["attemptedPages"]:
                current["attemptedPages"].append(page)
            maximum_pages = int(result.get("executionProfile", {}).get("maxPagesPerQuery") or 1)
            current["nextPage"] = min(page + 1, maximum_pages + 1)
            for group in admitted_physical_groups:
                value = str(group or "")
                if value and value not in current["admittedPhysicalGroups"]:
                    current["admittedPhysicalGroups"].append(value)
            known_rejections = {
                (str(item.get("physicalGroupId") or ""), str(item.get("reasonCode") or ""))
                for item in current["rejectedPhysicalGroups"]
                if isinstance(item, dict)
            }
            for item in rejected_physical_groups:
                if not isinstance(item, dict):
                    continue
                key_pair = (str(item.get("physicalGroupId") or ""), str(item.get("reasonCode") or ""))
                if key_pair[0] and key_pair not in known_rejections:
                    current["rejectedPhysicalGroups"].append(
                        {"physicalGroupId": key_pair[0], "reasonCode": key_pair[1]}
                    )
                    known_rejections.add(key_pair)
            current.pop("lastProviderOutcome", None)
            maximum_pages = int(result.get("executionProfile", {}).get("maxPagesPerQuery") or 1)
            if int(current.get("nextPage") or 1) > maximum_pages:
                result["remainingQueryScopes"] = [
                    item
                    for item in cls.normalize_remaining_query_scopes(result.get("remainingQueryScopes") or [])
                    if cls._slot_scope_identity(item) != cls._slot_scope_identity(query)
                ]
        else:
            current["lastProviderOutcome"] = "failure"
        cls._refresh_status(result)
        return result

    @classmethod
    def normalize_remaining_query_scopes(
        cls,
        scopes: Iterable[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Normalize the server-emitted exact-scope queue without collapsing one slot.

        A slot may legitimately have midpoint, predecessor and successor scopes,
        or several predecessor-beam scopes.  The exact scope fingerprint, not
        the slot id, is therefore the cursor identity.
        """

        normalized: list[dict[str, Any]] = []
        known: dict[tuple[int, str, str, str], dict[str, Any]] = {}
        for raw in scopes:
            if not isinstance(raw, dict):
                raise ValueError("simple_direction_remaining_query_scope_invalid")
            day_number = cls._positive_int(raw.get("dayNumber"))
            slot_id = str(raw.get("slotId") or "").strip()
            day_seed_amap_id = str(raw.get("daySeedAmapId") or "").strip().upper()
            query_scope_fingerprint = str(raw.get("queryScopeFingerprint") or "").strip()
            center_role = str(raw.get("centerRole") or "").strip().casefold()
            query_role = str(raw.get("queryRole") or "").strip()
            if (
                not day_number
                or not slot_id
                or not query_scope_fingerprint
                or center_role not in cls._CENTER_ROLES
                or query_role not in {"", "adjacent_candidate_center"}
                or (day_seed_amap_id and not cls.is_valid_amap_id(day_seed_amap_id))
            ):
                raise ValueError("simple_direction_remaining_query_scope_invalid")
            try:
                priority = max(0, int(raw.get("priority") or 0))
                predecessor_beam_rank = max(0, int(raw.get("predecessorBeamRank") or 0))
            except (TypeError, ValueError) as error:
                raise ValueError("simple_direction_remaining_query_scope_invalid") from error
            item = {
                "dayNumber": day_number,
                "slotId": slot_id,
                "daySeedAmapId": day_seed_amap_id,
                "queryScopeFingerprint": query_scope_fingerprint,
                "centerRole": center_role,
                "queryRole": query_role,
                "queryText": str(raw.get("queryText") or "").strip(),
                "priority": priority,
                "currentPartialCompletionSlot": raw.get("currentPartialCompletionSlot") is True,
                "predecessorAmapId": str(raw.get("predecessorAmapId") or "").strip().upper(),
                "successorAmapId": str(raw.get("successorAmapId") or "").strip().upper(),
                "predecessorBeamRank": predecessor_beam_rank,
                "initialPageAlreadyAttempted": raw.get("initialPageAlreadyAttempted") is True,
                "isActiveScope": raw.get("isActiveScope") is True,
                "attemptedThisTurn": raw.get("attemptedThisTurn") is True,
                "providerOutcome": str(raw.get("providerOutcome") or "").strip().casefold(),
                "remainingReason": str(raw.get("remainingReason") or "").strip(),
            }
            if item["providerOutcome"] not in {"", "success", "failure", "not_called"}:
                raise ValueError("simple_direction_remaining_query_scope_invalid")
            if item["remainingReason"] not in {
                "",
                "provider_failure",
                "no_candidate_selected",
                "not_executed",
            }:
                raise ValueError("simple_direction_remaining_query_scope_invalid")
            identity = cls._slot_scope_identity(item)
            if identity in known:
                if known[identity] != item:
                    raise ValueError("simple_direction_remaining_query_scope_conflict")
                continue
            known[identity] = item
            normalized.append(item)
        center_order = {"midpoint": 0, "predecessor": 1, "successor": 2, "": 3}
        normalized.sort(
            key=lambda item: (
                0 if item["currentPartialCompletionSlot"] else 1,
                0 if item["isActiveScope"] else 1,
                int(item["priority"]),
                int(item["dayNumber"]),
                str(item["slotId"]),
                center_order.get(str(item["centerRole"]), 4),
                int(item["predecessorBeamRank"]),
                str(item["queryScopeFingerprint"]),
            )
        )
        return normalized

    @classmethod
    def remaining_query_scopes(
        cls,
        frontier: dict[str, Any],
        *,
        allowed_day_seed_amap_ids: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        cls._validate(frontier)
        restrict_seeds = allowed_day_seed_amap_ids is not None
        allowed = {
            cls._canonical_amap_id(item)
            for item in (allowed_day_seed_amap_ids or set())
            if cls._canonical_amap_id(item)
        }
        remaining: list[dict[str, Any]] = []
        for scope in cls.normalize_remaining_query_scopes(frontier.get("remainingQueryScopes") or []):
            seed = cls._canonical_amap_id(scope.get("daySeedAmapId"))
            if restrict_seeds and seed not in allowed:
                continue
            query = cls.begin_slot_query(
                frontier,
                day_number=int(scope["dayNumber"]),
                slot_id=str(scope["slotId"]),
                day_seed_amap_id=str(scope["daySeedAmapId"]),
                query_scope_fingerprint=str(scope["queryScopeFingerprint"]),
            )
            if query.get("exhausted") is not True:
                remaining.append(copy.deepcopy(scope))
        return remaining

    @classmethod
    def remaining_query_page_count(cls, frontier: dict[str, Any]) -> int:
        """Count exact Provider pages still claimable across authoritative scopes."""

        cls._validate(frontier)
        maximum_pages = max(1, int((frontier.get("executionProfile") or {}).get("maxPagesPerQuery") or 1))
        remaining_pages = 0
        for scope in cls.remaining_query_scopes(frontier):
            query = cls.begin_slot_query(
                frontier,
                day_number=int(scope["dayNumber"]),
                slot_id=str(scope["slotId"]),
                day_seed_amap_id=str(scope["daySeedAmapId"]),
                query_scope_fingerprint=str(scope["queryScopeFingerprint"]),
            )
            page = max(1, int(query.get("page") or 1))
            if page <= maximum_pages:
                remaining_pages += maximum_pages - page + 1
        return remaining_pages

    @classmethod
    def claim_slot_queries(
        cls,
        frontier: dict[str, Any],
        *,
        allowed_day_seed_amap_ids: set[str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Return at most one exact active query per slot, ordered by completion priority."""

        cls._validate(frontier)
        restrict_seeds = allowed_day_seed_amap_ids is not None
        allowed = {
            cls._canonical_amap_id(item)
            for item in (allowed_day_seed_amap_ids or set())
            if cls._canonical_amap_id(item)
        }
        scopes_by_identity = {
            cls._slot_scope_identity(item): item
            for item in cls.normalize_remaining_query_scopes(frontier.get("remainingQueryScopes") or [])
        }
        authoritative_queue = frontier.get("remainingQueryScopesAuthoritative") is True
        for stored in (frontier.get("slotFrontiers") or {}).values():
            if not isinstance(stored, dict):
                continue
            identity = cls._slot_scope_identity(stored)
            if authoritative_queue and identity not in scopes_by_identity:
                continue
            if identity not in scopes_by_identity:
                scopes_by_identity[identity] = {
                    "dayNumber": int(stored.get("dayNumber") or 0),
                    "slotId": str(stored.get("slotId") or ""),
                    "daySeedAmapId": str(stored.get("daySeedAmapId") or ""),
                    "queryScopeFingerprint": str(stored.get("queryScopeFingerprint") or ""),
                    **cls._query_scope_metadata(stored),
                }
        ordered_scopes = cls.normalize_remaining_query_scopes(scopes_by_identity.values())
        claimed: dict[str, dict[str, Any]] = {}
        for scope in ordered_scopes:
            slot_id = str(scope["slotId"])
            seed = cls._canonical_amap_id(scope.get("daySeedAmapId"))
            if slot_id in claimed or (restrict_seeds and seed not in allowed):
                continue
            query = cls.begin_slot_query(
                frontier,
                day_number=int(scope["dayNumber"]),
                slot_id=slot_id,
                day_seed_amap_id=str(scope["daySeedAmapId"]),
                query_scope_fingerprint=str(scope["queryScopeFingerprint"]),
            )
            if query.get("exhausted") is True:
                continue
            claimed[slot_id] = {**copy.deepcopy(scope), **query}
        return claimed

    @staticmethod
    def _slot_scope_identity(value: dict[str, Any]) -> tuple[int, str, str, str]:
        return (
            int(value.get("dayNumber") or 0),
            str(value.get("slotId") or ""),
            str(value.get("daySeedAmapId") or "").strip().upper(),
            str(value.get("queryScopeFingerprint") or ""),
        )

    @staticmethod
    def _query_scope_metadata(value: dict[str, Any]) -> dict[str, Any]:
        return {
            "centerRole": str(value.get("centerRole") or ""),
            "queryRole": str(value.get("queryRole") or ""),
            "queryText": str(value.get("queryText") or ""),
            "priority": max(0, int(value.get("priority") or 0)),
            "currentPartialCompletionSlot": value.get("currentPartialCompletionSlot") is True,
            "predecessorAmapId": str(value.get("predecessorAmapId") or ""),
            "successorAmapId": str(value.get("successorAmapId") or ""),
            "predecessorBeamRank": max(0, int(value.get("predecessorBeamRank") or 0)),
            "initialPageAlreadyAttempted": value.get("initialPageAlreadyAttempted") is True,
            "isActiveScope": value.get("isActiveScope") is True,
            "attemptedThisTurn": value.get("attemptedThisTurn") is True,
            "providerOutcome": str(value.get("providerOutcome") or ""),
            "remainingReason": str(value.get("remainingReason") or ""),
        }

    @classmethod
    def _refresh_status(cls, frontier: dict[str, Any]) -> None:
        entities = [item for item in frontier.get("qualifiedEntityFrontier") or [] if isinstance(item, dict)]
        remaining = sum(str(item.get("state") or "") == "untried" for item in entities)
        frontier["remainingQualifiedEntityCount"] = remaining
        retryable_groups = cls._retryable_grounded_groups(frontier)
        remaining_poi_pages = cls._remaining_grounded_slot_pages(frontier, retryable_groups)
        authoritative_scope_pages = (
            cls.remaining_query_page_count(frontier)
            if frontier.get("remainingQueryScopesAuthoritative") is True
            else 0
        )
        remaining_poi_pages = max(remaining_poi_pages, authoritative_scope_pages)
        frontier["remainingPoiPageCount"] = remaining_poi_pages
        # A reconciled partial proposal can be blocked by its own route
        # Provider evidence while other frozen qualification entities remain
        # available for a different direction.  Only an unresolved Provider
        # call/claim blocks the whole frontier.  The terminal blocking layer is
        # still retained below for the case where no alternative frontier
        # remains.
        provider_pending = (
            frontier.get("providerRecoveryPending") is True
            or any(str(item.get("state") or "") == "grounding_pending" for item in entities)
            or any(
                str(item.get("lastProviderOutcome") or "") == "failure"
                for item in (frontier.get("slotFrontiers") or {}).values()
                if isinstance(item, dict)
            )
        )
        if provider_pending:
            status = "provider_pending"
        elif remaining or retryable_groups or authoritative_scope_pages:
            status = "has_more"
        else:
            terminal_layer = str(frontier.get("terminalBlockingLayer") or "qualification")
            status = {
                "provider": "provider_pending",
                "poi": "poi_exhausted",
                "route": "route_feasible_exhausted",
                "qualification": "qualification_exhausted",
            }.get(terminal_layer, "qualification_exhausted")
        frontier["frontierStatus"] = status
        frontier["frontierFingerprint"] = cls._frontier_fingerprint(frontier)

    @classmethod
    def _retryable_grounded_group(
        cls,
        frontier: dict[str, Any],
        *,
        required_size: int,
    ) -> list[dict[str, Any]]:
        groups = cls._retryable_grounded_groups(frontier)
        for group in groups:
            if len(group) >= required_size:
                ordered = sorted(
                    group,
                    key=lambda item: (
                        int(item.get("assignedDayNumber") or 0),
                        str(item.get("assignedSlotId") or ""),
                        str(item.get("evidenceEntityFingerprint") or ""),
                    ),
                )
                return ordered[:required_size]
        return []

    @classmethod
    def _retryable_grounded_groups(cls, frontier: dict[str, Any]) -> list[list[dict[str, Any]]]:
        entities = [
            item
            for item in frontier.get("qualifiedEntityFrontier") or []
            if isinstance(item, dict)
            and str(item.get("state") or "") in {"grounded", "assigned_partial"}
            and str(item.get("frontierAttemptGroupId") or "")
            and cls._canonical_amap_id(item.get("canonicalAmapId"))
        ]
        grouped: dict[str, list[dict[str, Any]]] = {}
        for entity in entities:
            grouped.setdefault(str(entity.get("frontierAttemptGroupId") or ""), []).append(entity)
        retryable: list[list[dict[str, Any]]] = []
        minimum_group_size = max(1, int(frontier.get("campusSlotCount") or 1))
        for group_id in sorted(grouped):
            group = grouped[group_id]
            amap_ids = {cls._canonical_amap_id(item.get("canonicalAmapId")) for item in group}
            slot_page_remaining = cls._slot_frontier_has_remaining_page(frontier, amap_ids=amap_ids)
            exact_scope_remaining = cls._remaining_exact_scope_exists(frontier, amap_ids=amap_ids)
            topology_remaining = any(
                cls._topology_frontier_has_remaining(item.get("continuationMetadata")) for item in group
            )
            if len(group) >= minimum_group_size and (
                slot_page_remaining or exact_scope_remaining or topology_remaining
            ):
                retryable.append(group)
        return retryable

    @staticmethod
    def _topology_frontier_has_remaining(value: Any) -> bool:
        if not isinstance(value, dict) or value.get("routeFeasibilityExhausted") is True:
            return False
        try:
            return int(value.get("unverifiedTopologyCombinationCount") or 0) > 0
        except (TypeError, ValueError):
            return False

    @classmethod
    def _slot_frontier_has_remaining_page(
        cls,
        frontier: dict[str, Any],
        *,
        amap_ids: set[str],
    ) -> bool:
        maximum_pages = max(
            1,
            int((frontier.get("executionProfile") or {}).get("maxPagesPerQuery") or 1),
        )
        authoritative_queue = frontier.get("remainingQueryScopesAuthoritative") is True
        queued_identities = {
            cls._slot_scope_identity(item)
            for item in cls.normalize_remaining_query_scopes(frontier.get("remainingQueryScopes") or [])
        }
        return any(
            cls._canonical_amap_id(item.get("daySeedAmapId")) in amap_ids
            and max(1, int(item.get("nextPage") or 1)) <= maximum_pages
            and (not authoritative_queue or cls._slot_scope_identity(item) in queued_identities)
            for item in (frontier.get("slotFrontiers") or {}).values()
            if isinstance(item, dict)
        )

    @classmethod
    def _remaining_exact_scope_exists(
        cls,
        frontier: dict[str, Any],
        *,
        amap_ids: set[str],
    ) -> bool:
        normalized_ids = {cls._canonical_amap_id(item) for item in amap_ids if cls._canonical_amap_id(item)}
        if not normalized_ids:
            return False
        return bool(
            cls.remaining_query_scopes(
                frontier,
                allowed_day_seed_amap_ids=normalized_ids,
            )
        )

    @classmethod
    def _remaining_grounded_slot_pages(
        cls,
        frontier: dict[str, Any],
        groups: list[list[dict[str, Any]]],
    ) -> int:
        maximum_pages = max(
            1,
            int((frontier.get("executionProfile") or {}).get("maxPagesPerQuery") or 1),
        )
        amap_ids = {
            cls._canonical_amap_id(item.get("canonicalAmapId"))
            for group in groups
            for item in group
            if cls._canonical_amap_id(item.get("canonicalAmapId"))
        }
        pages_by_scope: dict[tuple[int, str, str, str], int] = {}
        authoritative_queue = frontier.get("remainingQueryScopesAuthoritative") is True
        queued_identities = {
            cls._slot_scope_identity(item)
            for item in cls.normalize_remaining_query_scopes(frontier.get("remainingQueryScopes") or [])
        }
        for item in (frontier.get("slotFrontiers") or {}).values():
            if (
                not isinstance(item, dict)
                or cls._canonical_amap_id(item.get("daySeedAmapId")) not in amap_ids
                or (authoritative_queue and cls._slot_scope_identity(item) not in queued_identities)
            ):
                continue
            pages_by_scope[cls._slot_scope_identity(item)] = max(
                maximum_pages - max(1, int(item.get("nextPage") or 1)) + 1,
                0,
            )
        for item in cls.remaining_query_scopes(
            frontier,
            allowed_day_seed_amap_ids=amap_ids,
        ):
            pages_by_scope.setdefault(cls._slot_scope_identity(item), maximum_pages)
        return sum(pages_by_scope.values())

    @classmethod
    def _validate(cls, frontier: dict[str, Any]) -> None:
        if not isinstance(frontier, dict) or frontier.get("schemaVersion") != cls.SCHEMA_VERSION:
            raise ValueError("simple_direction_frontier_invalid")
        if "explorationOrdering" not in frontier:
            return
        ordering = frontier["explorationOrdering"]
        entities = frontier.get("qualifiedEntityFrontier")
        if (
            not isinstance(ordering, dict)
            or not isinstance(entities, list)
            or any(not isinstance(item, dict) for item in entities)
        ):
            raise ValueError("simple_direction_exploration_ordering_invalid")
        expected = cls._exploration_ordering(
            seed=ordering.get("selectionSeed"),
            entity_fingerprints=[str(item.get("evidenceEntityFingerprint") or "") for item in entities],
        )
        if ordering != expected:
            raise ValueError("simple_direction_exploration_ordering_mismatch")
        for entity in entities:
            binding = entity.get("qualificationBinding")
            reason = EntityQualificationEvidenceService.validate_binding(
                binding,
                expected_planning_root_id=str(frontier.get("planningRootId") or ""),
                expected_request_contract_fingerprint=str(frontier.get("requestContractFingerprint") or ""),
                expected_entity_fingerprint=str(entity.get("evidenceEntityFingerprint") or ""),
                expected_canonical_name=str(entity.get("canonicalName") or ""),
            )
            # Keep the established qualification failure taxonomy even when
            # a new ordering policy validates the binding before assignment.
            if reason == "qualification_binding_scope_mismatch":
                raise ValueError("simple_direction_qualification_binding_scope_mismatch")
            if reason == "qualification_binding_evidence_epoch_mismatch" or (
                not reason
                and isinstance(binding, dict)
                and str(binding.get("qualificationEvidenceFingerprint") or "")
                != str(frontier.get("qualificationEvidenceFingerprint") or "")
            ):
                raise ValueError("simple_direction_qualification_evidence_epoch_mismatch")
            if (
                reason
                or not isinstance(binding, dict)
                or str(binding.get("locality") or "") != str(entity.get("locality") or "")
            ):
                raise ValueError("simple_direction_exploration_entity_membership_invalid")

    @classmethod
    def _exploration_ordering(cls, *, seed: Any, entity_fingerprints: list[str]) -> dict[str, Any]:
        """Rank the qualified set using one explicit input, never audit identities."""

        if not isinstance(seed, str) or not re.fullmatch(r"[a-f0-9]{32}", seed):
            raise ValueError("simple_direction_exploration_seed_invalid")
        if len(set(entity_fingerprints)) != len(entity_fingerprints) or any(
            not re.fullmatch(r"[a-f0-9]{64}", value) for value in entity_fingerprints
        ):
            raise ValueError("simple_direction_exploration_entity_membership_invalid")
        ordered = sorted(
            entity_fingerprints,
            key=lambda value: (cls._fingerprint({"selectionSeed": seed, "entityFingerprint": value}), value),
        )
        material = {
            "schemaVersion": cls.EXPLORATION_ORDERING_VERSION,
            "selectionSeed": seed,
            "orderedEntityFingerprints": ordered,
        }
        return {**material, "orderingFingerprint": cls._fingerprint(material)}

    @classmethod
    def _frontier_fingerprint(cls, frontier: dict[str, Any]) -> str:
        material = copy.deepcopy(frontier)
        material.pop("frontierFingerprint", None)
        return cls._fingerprint(material)

    @staticmethod
    def _fingerprint(value: Any) -> str:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _normalize_locality(value: Any) -> str:
        return re.sub(r"(?:特别行政区|自治区|自治州|地区|盟|市)$", "", str(value or "").strip()).casefold()

    @staticmethod
    def _positive_int(value: Any) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return 0
        return parsed if parsed > 0 else 0

    @classmethod
    def _canonical_amap_id(cls, value: Any) -> str:
        normalized = str(value or "").strip().upper()
        return normalized if cls.is_valid_amap_id(normalized) else ""

    @classmethod
    def is_valid_amap_id(cls, value: Any) -> bool:
        """Return whether ``value`` is a canonical AMap POI identity."""

        return cls._AMAP_ID_RE.fullmatch(str(value or "").strip().upper()) is not None

    @classmethod
    def is_valid_provider_outcome(cls, value: Any) -> bool:
        return str(value or "").strip().casefold() in cls._PROVIDER_OUTCOMES

    @classmethod
    def is_valid_blocking_layer(cls, value: Any) -> bool:
        return str(value or "").strip().casefold() in cls._BLOCKING_LAYERS

from __future__ import annotations

import copy
import json
import sqlite3
from hashlib import sha256
from datetime import datetime, timezone
from typing import Callable, Optional

from fastapi import HTTPException

from src.api.schemas.maps import MapPoiResponse
from src.services.agent_verifier_service import AgentVerifierService
from src.services.itinerary_patch_service import ItineraryPatchService
from src.services.itinerary_snapshot_service import ItinerarySnapshotService
from src.services.route_insertion_scorer import RouteInsertionScorer
from src.services.timeline_mutation_compiler import TimelineMutationCompiler
from src.services.timeline_mutation_models import (
    BoundTimelineMutation,
    TimelineMutationIntent,
    TimelineMutationOutcome,
    TimelineMutationResolution,
)
from src.services.timeline_mutation_poi_resolver import TimelineMutationPoiResolver
from src.services.timeline_mutation_postcondition_verifier import TimelineMutationPostconditionVerifier
from src.services.timeline_target_binder import TimelineTargetBinder
from src.services.timeline_write_lease import timeline_write_lease


FailureInjector = Callable[[str], None]


class TimelineMutationTransactionService:
    def __init__(
        self,
        db: sqlite3.Connection,
        *,
        binder: Optional[TimelineTargetBinder] = None,
        resolver: Optional[TimelineMutationPoiResolver] = None,
        compiler: Optional[TimelineMutationCompiler] = None,
        postcondition_verifier: Optional[TimelineMutationPostconditionVerifier] = None,
        structural_verifier: Optional[AgentVerifierService] = None,
        patch_service: Optional[ItineraryPatchService] = None,
        failure_injector: Optional[FailureInjector] = None,
    ):
        self.db = db
        self.binder = binder or TimelineTargetBinder(db)
        self.resolver = resolver or TimelineMutationPoiResolver()
        self.compiler = compiler or TimelineMutationCompiler()
        self.postcondition_verifier = postcondition_verifier or TimelineMutationPostconditionVerifier()
        self.structural_verifier = structural_verifier or AgentVerifierService(db)
        self.patch_service = patch_service or ItineraryPatchService(db)
        self.failure_injector = failure_injector
        self.snapshots = ItinerarySnapshotService(db)

    def execute(
        self,
        session_id: str,
        intent: TimelineMutationIntent,
        *,
        source_turn_id: Optional[str] = None,
        selected_poi: Optional[MapPoiResponse] = None,
    ) -> TimelineMutationOutcome:
        existing = self._existing_outcome(session_id, intent)
        if existing is not None:
            return existing
        bound = self.binder.bind(session_id, intent)
        with timeline_write_lease(bound.plan_id):
            try:
                self.binder.assert_current(bound)
            except HTTPException as error:
                existing = self._existing_outcome(session_id, intent)
                if error.status_code == 409 and existing is not None:
                    return existing
                raise
            existing = self._existing_outcome(session_id, intent)
            if existing is not None:
                return existing
            return self._execute_bound(bound, source_turn_id=source_turn_id, selected_poi=selected_poi)

    def _execute_bound(
        self,
        bound: BoundTimelineMutation,
        *,
        source_turn_id: Optional[str] = None,
        selected_poi: Optional[MapPoiResponse] = None,
    ) -> TimelineMutationOutcome:
        intent = bound.intent
        events = [self._event("timeline_mutation_detected", "completed")]
        try:
            self._inject("after target binding")
        except Exception as error:
            return self._prewrite_failure(bound, source_turn_id, events, error)
        if bound.binding_status != "unique":
            event_name = (
                "timeline_target_ambiguous" if bound.binding_status == "ambiguous" else "timeline_target_not_found"
            )
            events.extend([self._event(event_name, "waiting"), self._event("active_version_unchanged", "completed")])
            outcome = TimelineMutationOutcome(
                mutationId=bound.mutation_id,
                status="needs_confirmation",
                operation=intent.operation,
                baseVersionId=bound.base_version_id,
                targetSegmentIds=bound.target_segment_ids,
                options=self._binding_options(bound),
                events=events,
                errorCode=bound.binding_status,
            )
            self._persist(bound, source_turn_id, outcome, binding=bound.model_dump(by_alias=True))
            return outcome
        events.append(self._event("timeline_target_bound", "completed"))

        before = self._version_snapshot(bound.base_version_id, bound.session_id)
        raw_route_decision_contract = before.get("routeDecisionContract") if isinstance(before, dict) else None
        validated_route_decision_contract = RouteInsertionScorer.normalized_route_decision_contract(
            raw_route_decision_contract
        )
        # The normalized form is only the fingerprinted policy core.  The
        # persisted server envelope also carries ready/missing/schema/source
        # truth consumed by save-back and later clarification gates.  Validate
        # with the core, but pass the exact server-owned envelope to the single
        # writer so an ordinary timeline edit cannot strip those fields.
        route_decision_contract = (
            copy.deepcopy(raw_route_decision_contract) if validated_route_decision_contract is not None else None
        )
        bound = bound.model_copy(update={"route_decision_contract": route_decision_contract})
        city = self._session_city(bound.session_id)
        resolution = (
            self.resolver.resolve_selected(bound, selected_poi, city=city)
            if selected_poi is not None
            else self.resolver.resolve(bound, city=city)
        )
        try:
            self._inject("after candidate resolution")
        except Exception as error:
            return self._prewrite_failure(bound, source_turn_id, events, error)
        if resolution.status in {"material_choice", "no_safe_candidate", "provider_failure"}:
            status = "needs_confirmation" if resolution.status == "material_choice" else "no_change"
            events.extend(
                [
                    self._event(
                        "timeline_replacement_unresolved", "waiting" if status == "needs_confirmation" else "failed"
                    ),
                    self._event("active_version_unchanged", "completed"),
                ]
            )
            outcome = TimelineMutationOutcome(
                mutationId=bound.mutation_id,
                status=status,
                operation=intent.operation,
                baseVersionId=bound.base_version_id,
                targetSegmentIds=bound.target_segment_ids,
                options=self._replacement_options(bound, resolution),
                events=events,
                errorCode=resolution.status,
                warnings=[resolution.failure_reason] if resolution.failure_reason else [],
            )
            self._persist(
                bound,
                source_turn_id,
                outcome,
                binding=bound.model_dump(by_alias=True),
                resolvedReplacement=resolution.model_dump(by_alias=True),
            )
            return outcome
        events.append(self._event("timeline_replacement_resolved", "completed"))

        if self._is_noop(bound, resolution):
            events.extend(
                [
                    self._event("timeline_mutation_no_change", "completed"),
                    self._event("active_version_unchanged", "completed"),
                ]
            )
            outcome = TimelineMutationOutcome(
                mutationId=bound.mutation_id,
                status="no_change",
                operation=intent.operation,
                baseVersionId=bound.base_version_id,
                targetSegmentIds=bound.target_segment_ids,
                events=events,
                errorCode="mutation_already_applied",
            )
            self._persist(
                bound,
                source_turn_id,
                outcome,
                binding=bound.model_dump(by_alias=True),
                resolvedReplacement=resolution.model_dump(by_alias=True),
            )
            return outcome

        compiled = self.compiler.compile(bound, resolution, before_snapshot=before)
        events.append(self._event("timeline_patch_compiled", "completed"))
        self.binder.assert_current(bound)
        base_versions = self._ids("itinerary_versions", bound.session_id)
        base_patches = self._ids("itinerary_patches", bound.session_id)
        patch_response = None
        structural = None
        report = None
        route_refresh_scope: dict = {}
        try:
            events.append(self._event("timeline_patch_started", "querying"))
            events.append(self._event("route_refresh_started", "querying"))
            patch_response = self.patch_service.apply_patch(
                bound.plan_id,
                compiled.operations,
                source_type="user_timeline_mutation",
                base_version_id=bound.base_version_id,
                source_turn_id=source_turn_id,
                planning_context={
                    "sessionId": bound.session_id,
                    "taskType": "local_modification",
                    "toolRefreshPolicy": {"route": "touched_pairs_only"},
                    "skipOptionalToolRefresh": True,
                    "timelineMutation": {
                        "mutationId": bound.mutation_id,
                        "pendingSlotSelection": (
                            intent.pending_slot_selection.model_dump(by_alias=True)
                            if intent.pending_slot_selection is not None
                            else None
                        ),
                    },
                    "pendingSlotSelection": (
                        intent.pending_slot_selection.model_dump(by_alias=True)
                        if intent.pending_slot_selection is not None
                        else None
                    ),
                    "pendingPoiCandidateId": (
                        intent.pending_slot_selection.candidate_record_id
                        if intent.pending_slot_selection is not None
                        else None
                    ),
                    "timelineMutationFailureInjector": self.failure_injector,
                },
                server_route_decision_contract=bound.route_decision_contract,
            )
            result_version_id = patch_response.version.id
            patch_id = patch_response.patch.id
            route_refresh_scope = patch_response.patch.metadata.get("routeRefreshScope", {})
            events.extend(
                [
                    self._event("route_refresh_completed", "completed"),
                    self._event("schedule_recomputed", "completed"),
                ]
            )
            current_versions = self._ids("itinerary_versions", bound.session_id)
            current_patches = self._ids("itinerary_patches", bound.session_id)
            created_versions = current_versions - base_versions
            created_patches = current_patches - base_patches
            if created_versions != {result_version_id}:
                raise RuntimeError(
                    f"mutation_version_delta_invalid: expected {[result_version_id]}, got {sorted(created_versions)}"
                )
            if created_patches != {patch_id}:
                raise RuntimeError(
                    f"mutation_patch_delta_invalid: expected {[patch_id]}, got {sorted(created_patches)}"
                )
            after = self._version_snapshot(result_version_id, bound.session_id)
            self._inject("before structural verifier")
            structural = self.structural_verifier.verify_agent_write(
                bound.session_id,
                bound.plan_id,
                result_version_id,
                patch_id,
                planning_context={
                    "taskType": "local_modification",
                    # The patch service derives this marker from the trusted base
                    # snapshot.  Never accept a client-provided profile override.
                    "simpleOpenRoutePolicyAuthorized": patch_response.patch.metadata.get(
                        "simpleOpenRouteStatus"
                    )
                    in {"ready", "partial", "provider_failed", "unknown"},
                },
            )
            if not structural.passed:
                raise RuntimeError("structural_verifier_failed: " + "; ".join(structural.hard_failures))
            events.append(self._event("structural_verifier", "completed"))
            self._inject("before postcondition verifier")
            report = self.postcondition_verifier.verify(
                before,
                after,
                compiled.postcondition,
                version_delta=len(created_versions),
            )
            if not report.passed:
                raise RuntimeError("mutation_postcondition_failed: " + "; ".join(report.errors))
            requested_route_pairs = {
                tuple(str(part) for part in pair)
                for pair in route_refresh_scope.get("pairs") or []
                if isinstance(pair, (list, tuple)) and len(pair) == 2
            }
            changed_route_pairs = {tuple(pair) for pair in report.diff.changed_route_pair_ids}
            if changed_route_pairs - requested_route_pairs:
                raise RuntimeError(
                    "mutation_postcondition_failed: route changes escaped requested scope: "
                    + str(sorted(changed_route_pairs - requested_route_pairs))
                )
            events.extend(
                [
                    self._event("mutation_postcondition_verifier", "completed"),
                    self._event("timeline_mutation_committed", "completed"),
                ]
            )
            outcome = TimelineMutationOutcome(
                mutationId=bound.mutation_id,
                status="success",
                operation=intent.operation,
                baseVersionId=bound.base_version_id,
                resultVersionId=result_version_id,
                patchId=patch_id,
                targetSegmentIds=bound.target_segment_ids or report.diff.direct_changed_segment_ids,
                changeSummary=self._change_summary(bound, after, report.diff.direct_changed_segment_ids),
                directChangedSegmentIds=report.diff.direct_changed_segment_ids,
                derivedChangedSegmentIds=report.diff.derived_changed_segment_ids,
                unexpectedChangedSegmentIds=report.diff.unexpected_changed_segment_ids,
                touchedRoutePairs=list(route_refresh_scope.get("pairs") or []),
                changedRoutePairIds=report.diff.changed_route_pair_ids,
                routeWriteDelta=len(report.diff.changed_route_pair_ids),
                routeStatus=route_refresh_scope.get("status"),
                postconditionPassed=True,
                structuralVerifierPassed=True,
                rollbackPerformed=False,
                warnings=list(patch_response.itinerary.route_warnings),
                events=events,
            )
            self._persist(
                bound,
                source_turn_id,
                outcome,
                binding=bound.model_dump(by_alias=True),
                resolvedReplacement=resolution.model_dump(by_alias=True),
                compiledOperations=[item.model_dump(by_alias=True, exclude_none=True) for item in compiled.operations],
                routeRefreshScope=route_refresh_scope,
                beforeAfterDiff=report.diff.model_dump(by_alias=True),
                structuralVerifier={
                    **structural.as_metadata(),
                },
                postconditionVerifier={"passed": True, "errors": []},
            )
            return outcome
        except Exception as error:
            owned_patch_ids = self._mutation_patch_ids(bound.mutation_id)
            if isinstance(error, HTTPException) and error.status_code == 409 and not owned_patch_ids:
                raise
            validation_errors = (
                list(structural.hard_failures)
                if structural is not None and not structural.passed
                else list(report.errors)
                if report is not None and not report.passed
                else [str(error)]
            )
            self._restore_failed_mutation(bound, before, owned_patch_ids, validation_errors)
            events.extend(
                [
                    self._event("timeline_mutation_failed", "failed"),
                    self._event("rollback_completed", "completed"),
                    self._event("active_version_restored", "completed"),
                ]
            )
            outcome = TimelineMutationOutcome(
                mutationId=bound.mutation_id,
                status="rolled_back",
                operation=intent.operation,
                baseVersionId=bound.base_version_id,
                targetSegmentIds=bound.target_segment_ids,
                rollbackPerformed=True,
                warnings=[str(error)],
                events=events,
                errorCode=self._error_code(error),
            )
            self._persist(
                bound,
                source_turn_id,
                outcome,
                binding=bound.model_dump(by_alias=True),
                resolvedReplacement=resolution.model_dump(by_alias=True),
                compiledOperations=[item.model_dump(by_alias=True, exclude_none=True) for item in compiled.operations],
                routeRefreshScope=route_refresh_scope,
                beforeAfterDiff=report.diff.model_dump(by_alias=True) if report is not None else {},
                structuralVerifier=structural.as_metadata() if structural is not None else {},
                postconditionVerifier={
                    "passed": False,
                    "errors": list(report.errors) if report is not None else [str(error)],
                },
            )
            return outcome

    def _restore_failed_mutation(
        self,
        bound: BoundTimelineMutation,
        before: dict,
        owned_patch_ids: set[str],
        validation_errors: list[str],
    ) -> None:
        self.db.rollback()
        if not owned_patch_ids:
            return
        self.snapshots.apply_snapshot(bound.plan_id, before)
        owned_version_ids = {
            str(row["id"])
            for row in self.db.execute(
                f"SELECT id FROM itinerary_versions WHERE session_id = ? AND source_patch_id IN ({','.join('?' for _ in owned_patch_ids)})",
                (bound.session_id, *owned_patch_ids),
            ).fetchall()
        }
        if owned_version_ids:
            placeholders = ",".join("?" for _ in owned_version_ids)
            self.db.execute(
                f"DELETE FROM itinerary_versions WHERE session_id = ? AND id IN ({placeholders})",
                (bound.session_id, *owned_version_ids),
            )
        if owned_patch_ids:
            placeholders = ",".join("?" for _ in owned_patch_ids)
            self.db.execute(
                f"UPDATE itinerary_patches SET validation_status = 'rolled_back_by_timeline_mutation', "
                f"validation_errors_json = ?, result_version_id = NULL "
                f"WHERE session_id = ? AND id IN ({placeholders})",
                (json.dumps(validation_errors, ensure_ascii=False), bound.session_id, *owned_patch_ids),
            )
        self.db.execute(
            "UPDATE conversation_sessions SET active_version_id = ?, updated_at = ? WHERE id = ?",
            (bound.base_version_id, datetime.now(timezone.utc).isoformat(), bound.session_id),
        )
        self.db.commit()

    @staticmethod
    def _change_summary(
        bound: BoundTimelineMutation,
        after: dict,
        direct_changed_segment_ids: list[str],
    ) -> dict:
        if bound.intent.operation == "add_segment":
            added_id = direct_changed_segment_ids[0] if direct_changed_segment_ids else ""
            added = next(
                (
                    segment
                    for day in after.get("days") or []
                    for segment in day.get("segments") or []
                    if segment.get("id") == added_id
                ),
                None,
            )
            poi = added.get("poi") if isinstance(added, dict) and isinstance(added.get("poi"), dict) else {}
            return {
                "dayNumber": bound.intent.selector.day_number,
                "addedSegmentId": added_id or None,
                "afterStartTime": added.get("startTime") if isinstance(added, dict) else None,
                "afterEndTime": added.get("endTime") if isinstance(added, dict) else None,
                "afterPoiName": poi.get("name"),
                "afterTransportMode": added.get("transportMode") if isinstance(added, dict) else None,
            }
        descriptor = bound.target_descriptors[0]
        after_segment = next(
            (
                segment
                for day in after.get("days") or []
                for segment in day.get("segments") or []
                if segment.get("id") == descriptor.segment_id
            ),
            None,
        )
        after_poi = (
            after_segment.get("poi")
            if isinstance(after_segment, dict) and isinstance(after_segment.get("poi"), dict)
            else {}
        )
        return {
            "dayNumber": descriptor.day_number,
            "beforeStartTime": descriptor.start_time,
            "beforeEndTime": descriptor.end_time,
            "afterStartTime": after_segment.get("startTime") if isinstance(after_segment, dict) else None,
            "afterEndTime": after_segment.get("endTime") if isinstance(after_segment, dict) else None,
            "beforePoiName": descriptor.poi_name,
            "afterPoiName": after_poi.get("name"),
            "beforeTransportMode": descriptor.transport_mode,
            "afterTransportMode": after_segment.get("transportMode") if isinstance(after_segment, dict) else None,
        }

    def _prewrite_failure(
        self,
        bound: BoundTimelineMutation,
        source_turn_id: Optional[str],
        events: list[dict],
        error: Exception,
    ) -> TimelineMutationOutcome:
        events.extend(
            [
                self._event("timeline_mutation_failed", "failed"),
                self._event("active_version_unchanged", "completed"),
            ]
        )
        outcome = TimelineMutationOutcome(
            mutationId=bound.mutation_id,
            status="failed",
            operation=bound.intent.operation,
            baseVersionId=bound.base_version_id,
            targetSegmentIds=bound.target_segment_ids,
            warnings=[str(error)],
            events=events,
            errorCode="timeline_mutation_prewrite_failed",
        )
        self._persist(bound, source_turn_id, outcome, binding=bound.model_dump(by_alias=True))
        return outcome

    def _persist(
        self, bound: BoundTimelineMutation, source_turn_id: Optional[str], outcome: TimelineMutationOutcome, **parts
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        payload = {
            "mutationId": bound.mutation_id,
            "sourceTurnId": source_turn_id,
            "intent": bound.intent.model_dump(by_alias=True),
            "baseVersionId": bound.base_version_id,
            "resultVersionId": outcome.result_version_id,
            "patchId": outcome.patch_id,
            "status": outcome.status,
            "rollbackPerformed": outcome.rollback_performed,
            "outcome": outcome.model_dump(by_alias=True),
            "requestFingerprint": self._request_fingerprint(bound.intent),
            "resolvedReplacement": {},
            "compiledOperations": [],
            "routeRefreshScope": {},
            "beforeAfterDiff": {},
            "structuralVerifier": {},
            "postconditionVerifier": {},
            **parts,
        }
        self.db.execute(
            """
            INSERT OR REPLACE INTO timeline_mutation_transactions (
                mutation_id, session_id, source_turn_id, plan_id, base_version_id,
                result_version_id, patch_id, status, transaction_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE((SELECT created_at FROM timeline_mutation_transactions WHERE mutation_id = ?), ?), ?)
            """,
            (
                bound.mutation_id,
                bound.session_id,
                source_turn_id,
                bound.plan_id,
                bound.base_version_id,
                outcome.result_version_id,
                outcome.patch_id,
                outcome.status,
                json.dumps(payload, ensure_ascii=False, default=str),
                bound.mutation_id,
                now,
                now,
            ),
        )
        self.db.commit()

    def _is_noop(self, bound: BoundTimelineMutation, resolution: TimelineMutationResolution) -> bool:
        if bound.intent.operation == "add_segment":
            return False
        target = bound.target_descriptors[0]
        replacement = bound.intent.replacement
        if bound.intent.operation == "replace_poi":
            return bool(resolution.selected_poi and target.poi_amap_id == resolution.selected_poi.id)
        if bound.intent.operation == "set_start_time":
            return target.start_time == replacement.start_time
        if bound.intent.operation == "set_duration":
            return self._minutes(target.end_time) - self._minutes(target.start_time) == replacement.duration_minutes
        if bound.intent.operation == "set_transport_mode":
            return target.transport_mode == replacement.transport_mode
        return False

    @staticmethod
    def _binding_options(bound: BoundTimelineMutation) -> list[dict]:
        options = [
            {
                "kind": "timeline_mutation_target",
                "id": f"{bound.mutation_id}:segment:{item.segment_id}",
                "label": f"Day {item.day_number} · {item.start_time}-{item.end_time} · {item.poi_name} · {item.intent_type or item.kind}",
                "value": item.poi_name,
                "action": "execute_timeline_mutation_choice",
                "expectedBaseVersionId": bound.base_version_id,
                "segmentId": item.segment_id,
                "dayNumber": item.day_number,
                "startTime": item.start_time,
                "poiName": item.poi_name,
                "intentType": item.intent_type,
                "mutationIntent": bound.intent.model_copy(
                    update={
                        "selector": bound.intent.selector.model_copy(
                            update={
                                "day_number": item.day_number,
                                "time_window": f"{item.start_time}-{item.end_time}",
                                "current_text": item.poi_name,
                                "intent_type": item.intent_type,
                                "ordinal": None,
                            }
                        ),
                        "source": "structured_ui",
                    }
                ).model_dump(by_alias=True),
            }
            for item in bound.target_descriptors
        ]
        options.append(
            {
                "kind": "custom_input",
                "id": f"{bound.mutation_id}:manual",
                "label": "手动描述目标",
                "action": "execute_timeline_mutation_manual",
                "expectedBaseVersionId": bound.base_version_id,
            }
        )
        return options

    @staticmethod
    def _replacement_options(
        bound: BoundTimelineMutation,
        resolution: TimelineMutationResolution,
    ) -> list[dict]:
        options: list[dict] = []
        for poi in resolution.candidates[:3]:
            detail = " · ".join(item for item in (poi.district, poi.address) if item)
            options.append(
                {
                    "kind": "timeline_mutation_poi",
                    "id": f"{bound.mutation_id}:poi:{poi.id}",
                    "label": f"{poi.name}{f' · {detail}' if detail else ''}",
                    "value": poi.name,
                    "action": "execute_timeline_mutation_choice",
                    "expectedBaseVersionId": bound.base_version_id,
                    "mutationIntent": bound.intent.model_copy(update={"source": "structured_ui"}).model_dump(
                        by_alias=True
                    ),
                    "amapId": poi.id,
                    "amapPoi": poi.model_dump(by_alias=True),
                }
            )
        options.append(
            {
                "kind": "custom_input",
                "id": f"{bound.mutation_id}:manual",
                "label": "我自己填写具体地点",
                "action": "execute_timeline_mutation_manual",
                "expectedBaseVersionId": bound.base_version_id,
            }
        )
        return options

    def _session_city(self, session_id: str) -> str:
        row = self.db.execute("SELECT city FROM conversation_sessions WHERE id = ?", (session_id,)).fetchone()
        return str(row["city"] or "") if row else ""

    def _version_snapshot(self, version_id: str, session_id: str) -> dict:
        row = self.db.execute(
            "SELECT snapshot_json FROM itinerary_versions WHERE id = ? AND session_id = ?", (version_id, session_id)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=409, detail={"code": "active_version_snapshot_missing"})
        return json.loads(row["snapshot_json"] or "{}")

    def _ids(self, table: str, session_id: str) -> set[str]:
        return {
            str(row["id"])
            for row in self.db.execute(f"SELECT id FROM {table} WHERE session_id = ?", (session_id,)).fetchall()
        }

    def _mutation_patch_ids(self, mutation_id: str) -> set[str]:
        return {
            str(row["id"])
            for row in self.db.execute(
                "SELECT id FROM itinerary_patches WHERE mutation_id = ?",
                (mutation_id,),
            ).fetchall()
        }

    def _existing_outcome(
        self,
        session_id: str,
        intent: TimelineMutationIntent,
    ) -> Optional[TimelineMutationOutcome]:
        session = self.db.execute(
            "SELECT active_version_id FROM conversation_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        active_version_id = str(session["active_version_id"] or "") if session else ""
        fingerprint = self._request_fingerprint(intent)
        rows = self.db.execute(
            "SELECT transaction_json FROM timeline_mutation_transactions WHERE session_id = ? AND status = 'success' ORDER BY created_at DESC",
            (session_id,),
        ).fetchall()
        for row in rows:
            try:
                payload = json.loads(row["transaction_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            if payload.get("requestFingerprint") != fingerprint:
                continue
            if str(payload.get("resultVersionId") or "") != active_version_id:
                continue
            prior = TimelineMutationOutcome.model_validate(payload.get("outcome") or {})
            return prior.model_copy(
                update={
                    "status": "no_change",
                    "existing_outcome": True,
                    "postcondition_passed": True,
                    "structural_verifier_passed": True,
                    "events": [
                        self._event("timeline_mutation_no_change", "completed"),
                        self._event("active_version_unchanged", "completed"),
                    ],
                    "error_code": "existing_mutation_outcome",
                }
            )
        return None

    @staticmethod
    def _request_fingerprint(intent: TimelineMutationIntent) -> str:
        if intent.pending_slot_selection is not None:
            payload = {
                "schemaVersion": intent.schema_version,
                "operation": intent.operation,
                "pendingSlotSelection": intent.pending_slot_selection.model_dump(by_alias=True),
            }
        else:
            payload = intent.model_dump(by_alias=True, exclude={"confidence", "source"})
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return sha256(canonical.encode("utf-8")).hexdigest()

    def _inject(self, stage: str) -> None:
        if self.failure_injector:
            self.failure_injector(stage)

    @staticmethod
    def _event(name: str, status: str) -> dict:
        return {
            "type": "timeline_mutation",
            "name": name,
            "status": status,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    @staticmethod
    def _minutes(value: str) -> int:
        hour, minute = value.split(":", 1)
        return int(hour) * 60 + int(minute)

    @staticmethod
    def _error_code(error: Exception) -> str:
        text = str(error)
        if text.startswith("structural_verifier_failed"):
            return "structural_verifier_failed"
        if text.startswith("mutation_postcondition_failed"):
            return "mutation_postcondition_failed"
        return "timeline_mutation_failed"

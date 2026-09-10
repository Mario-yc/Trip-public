"""One bounded, zero-write completion attempt for a selected creative theme."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from contextvars import copy_context
import copy
from hashlib import sha256
from time import monotonic as default_monotonic
from typing import Any, Callable, Iterable, Optional
from uuid import uuid4

from src.services.candidate_provider_evidence_service import (
    CandidateProviderEvidenceService,
)
from src.services.consumer_candidate_admission_service import (
    ConsumerCandidateAdmissionService,
)
from src.services.creative_output_quality_service import CreativeOutputQualityService, ThemeCompletionBudget
from src.services.poi_physical_identity_service import PoiPhysicalIdentityService


class CreativeThemeCompletionService:
    def __init__(self, budget: ThemeCompletionBudget | None = None) -> None:
        self.budget = budget or ThemeCompletionBudget()

    def complete(
        self,
        *,
        meal_candidates_by_slot: dict[str, list[dict[str, Any]]],
        area_candidates: list[dict[str, Any]],
        detail_fetcher: Callable[[dict[str, Any]], dict[str, Any]],
        identity_web_fetcher: Callable[[dict[str, Any]], dict[str, Any]],
        admission: Callable[[dict[str, Any]], bool],
        monotonic: Callable[[], float] = default_monotonic,
        attempt_id: str | None = None,
        attempt_count: int = 1,
    ) -> dict[str, Any]:
        started = monotonic()
        attempt_id = str(attempt_id or f"theme_completion_{uuid4().hex[:16]}")
        trace: dict[str, Any] = {
            **self.budget.to_trace(),
            "themeCompletionAttemptId": attempt_id,
            "themeCompletionAttemptCount": max(1, int(attempt_count)),
            "themeCompletionElapsedMs": 0,
            "themeCompletionTerminalStatus": "running",
            "amapDetailPlannedCount": 0,
            "amapDetailExecutedCount": 0,
            "identitySpecificWebPlannedCount": 0,
            "identitySpecificWebExecutedCount": 0,
            "amapDetailAttemptCount": 0,
            "identitySpecificWebQueryCount": 0,
            "autoAttemptCount": 1,
            "versionWriteCount": 0,
            "patchWriteCount": 0,
            "themeCompletionWriteCount": 0,
            "replacementPreviewWriteCount": 0,
            "deadlineExceeded": False,
            "mealFairnessOrder": [],
            "mealFairFirstPassCount": 0,
            "budgetSkippedReasonExact": None,
            "remainingCompletionMs": int(self.budget.deadline_seconds * 1000),
        }
        admitted: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []
        provider_failed = False
        provider_reason_code = ""
        precondition_failed = False
        budget_exhausted = False
        candidates = self._fair_candidate_order(
            meal_candidates_by_slot,
            area_candidates,
        )
        trace["amapDetailPlannedCount"] = min(
            len(candidates),
            self.budget.amap_detail_limit,
        )
        detailed: list[dict[str, Any]] = []

        for slot_id, raw in candidates:
            if monotonic() - started >= self.budget.deadline_seconds:
                trace["deadlineExceeded"] = True
                pending.append(raw)
                break
            if trace["amapDetailAttemptCount"] >= self.budget.amap_detail_limit:
                budget_exhausted = True
                trace["budgetSkippedReasonExact"] = "global_call_budget_exhausted"
                pending.append(raw)
                continue
            amap_id = str(raw.get("amapId") or raw.get("id") or "").strip()
            if not amap_id:
                pending.append({**raw, "completionReasonCode": "amap_identity_missing"})
                precondition_failed = True
                continue
            trace["amapDetailAttemptCount"] += 1
            try:
                enriched = self._invoke_bounded(
                    detail_fetcher,
                    dict(raw),
                    timeout_seconds=max(
                        0.0,
                        self.budget.deadline_seconds - (monotonic() - started),
                    ),
                )
                trace["amapDetailExecutedCount"] += 1
            except FutureTimeoutError:
                trace["deadlineExceeded"] = True
                trace["budgetSkippedReasonExact"] = "deadline_exhausted"
                pending.append({**raw, "completionReasonCode": "deadline_exhausted"})
                break
            except Exception:
                provider_failed = True
                provider_reason_code = "amap_detail_provider_failed"
                pending.append({**raw, "completionReasonCode": "amap_detail_provider_failed"})
                continue
            if slot_id:
                if slot_id not in trace["mealFairnessOrder"]:
                    trace["mealFairFirstPassCount"] += 1
                trace["mealFairnessOrder"].append(slot_id)
            detailed.append(enriched if isinstance(enriched, dict) else dict(raw))

        for candidate in detailed:
            if admission(candidate):
                admitted.append(candidate)
                continue
            if monotonic() - started >= self.budget.deadline_seconds:
                trace["deadlineExceeded"] = True
                pending.append(candidate)
                continue
            amap_id = str(candidate.get("amapId") or candidate.get("id") or "").strip()
            if not amap_id or trace["identitySpecificWebQueryCount"] >= self.budget.identity_web_limit:
                if amap_id:
                    budget_exhausted = True
                    trace["budgetSkippedReasonExact"] = "web_seed_budget_exhausted"
                else:
                    precondition_failed = True
                pending.append(candidate)
                continue
            trace["identitySpecificWebPlannedCount"] += 1
            trace["identitySpecificWebQueryCount"] += 1
            try:
                claim = self._invoke_bounded(
                    identity_web_fetcher,
                    dict(candidate),
                    timeout_seconds=max(
                        0.0,
                        self.budget.deadline_seconds - (monotonic() - started),
                    ),
                )
                trace["identitySpecificWebExecutedCount"] += 1
            except FutureTimeoutError:
                trace["deadlineExceeded"] = True
                trace["budgetSkippedReasonExact"] = "deadline_exhausted"
                pending.append({**candidate, "completionReasonCode": "deadline_exhausted"})
                break
            except Exception:
                provider_failed = True
                provider_reason_code = "identity_web_provider_failed"
                pending.append({**candidate, "completionReasonCode": "identity_web_provider_failed"})
                continue
            merged = {
                **candidate,
                "identitySpecificWebClaim": claim if isinstance(claim, dict) else {},
            }
            if (
                isinstance(claim, dict)
                and claim.get("identityBound") is True
                and str(claim.get("amapId") or amap_id) == amap_id
                and admission(merged)
            ):
                admitted.append(merged)
            else:
                pending.append({**merged, "completionReasonCode": "identity_web_claim_insufficient"})

        if monotonic() - started >= self.budget.deadline_seconds:
            trace["deadlineExceeded"] = True
        if trace["deadlineExceeded"]:
            status, reason_code = "deadline_reached", "deadline_exhausted"
        elif admitted and not pending:
            status, reason_code = "completed", "theme_completion_completed"
        elif admitted:
            status, reason_code = (
                "partial_but_theme_ineligible",
                "theme_evidence_partial",
            )
        elif provider_failed:
            status, reason_code = (
                "provider_failed",
                provider_reason_code or "completion_provider_failed",
            )
        elif budget_exhausted:
            status, reason_code = (
                "budget_exhausted",
                str(trace.get("budgetSkippedReasonExact") or "global_call_budget_exhausted"),
            )
        elif precondition_failed or pending:
            status, reason_code = "precondition_failed", "amap_identity_missing"
        else:
            status, reason_code = "precondition_failed", "completion_candidates_missing"
        elapsed = max(0.0, monotonic() - started)
        trace["themeCompletionElapsedMs"] = min(
            int(round(elapsed * 1000)),
            int(self.budget.deadline_seconds * 1000),
        )
        trace["remainingCompletionMs"] = max(
            0,
            int(round((self.budget.deadline_seconds - elapsed) * 1000)),
        )
        trace["themeCompletionTerminalStatus"] = status
        return {
            "status": status,
            "reasonCode": reason_code,
            "admittedAdditions": admitted,
            "pendingCandidates": pending,
            "trace": trace,
        }

    @staticmethod
    def apply_admitted_additions(
        snapshot: dict[str, Any],
        additions: Iterable[dict[str, Any]],
    ) -> tuple[dict[str, Any], list[str]]:
        """Materialize admitted AMap facts in proposal memory, never persistence."""

        from src.services.proposal_readiness_service import ProposalReadinessService

        projected = copy.deepcopy(snapshot)
        before_pairs = {
            (str(item["fromSegmentId"]), str(item["toSegmentId"]))
            for item in ProposalReadinessService.expected_route_pairs(projected)
        }
        pending = [item for item in projected.get("portfolioPendingSlots") or [] if isinstance(item, dict)]
        existing_physical_ids = {
            PoiPhysicalIdentityService.canonical_amap_id(segment.get("poi") or {})
            for day in projected.get("days") or []
            if isinstance(day, dict)
            for segment in day.get("segments") or []
            if isinstance(segment, dict)
        }
        existing_physical_ids.discard("")
        added_ids: list[str] = []
        for candidate in additions:
            if not isinstance(candidate, dict):
                continue
            amap_id = str(candidate.get("amapId") or candidate.get("id") or "").strip()
            slot_id = str(candidate.get("planningSlotId") or candidate.get("slotId") or "").strip()
            report = (
                candidate.get("consumerAdmissionReport")
                if isinstance(candidate.get("consumerAdmissionReport"), dict)
                else {}
            )
            canonical_candidate = {
                **candidate,
                "amapId": amap_id,
            }
            physical_id = PoiPhysicalIdentityService.canonical_amap_id(canonical_candidate)
            if (
                not CreativeOutputQualityService.is_canonical_amap_poi(canonical_candidate)
                or PoiPhysicalIdentityService.invalid_parent_id(canonical_candidate)
                or not physical_id
                or physical_id in existing_physical_ids
                or not slot_id
                or not (
                    report.get("scoreEligible") is True
                    or str(report.get("classification") or "")
                    in {"admitted_final_anchor", "admitted_anchor_set_member"}
                )
            ):
                continue
            slot = next(
                (item for item in pending if str(item.get("planningSlotId") or item.get("slotId") or "") == slot_id),
                None,
            )
            if slot is None:
                continue
            day_number = int(slot.get("dayNumber") or candidate.get("dayNumber") or 0)
            day = next(
                (
                    item
                    for item in projected.get("days") or []
                    if isinstance(item, dict) and int(item.get("dayNumber") or 0) == day_number
                ),
                None,
            )
            if day is None:
                continue
            start_time = str(slot.get("startTime") or candidate.get("startTime") or "").strip()
            duration = int(slot.get("durationMinutes") or candidate.get("durationMinutes") or 60)
            end_time = str(slot.get("endTime") or "").strip()
            if start_time and not end_time:
                end_time = CreativeThemeCompletionService._add_minutes(start_time, duration)
            family = str(
                candidate.get("optionalExperienceFamily")
                or slot.get("optionalExperienceFamily")
                or candidate.get("intentType")
                or slot.get("intentType")
                or ""
            )
            raw_coverage_roles = candidate.get("coverageRoles") or slot.get("coverageRoles") or []
            coverage_roles = (
                list(dict.fromkeys(str(role) for role in raw_coverage_roles))[:3]
                if isinstance(raw_coverage_roles, (list, tuple, set))
                else []
            )
            raw_role_reports = (
                candidate.get("coverageRoleAdmissionReports") or slot.get("coverageRoleAdmissionReports") or {}
            )
            coverage_role_reports = {
                role: copy.deepcopy(raw_role_reports[role])
                for role in coverage_roles
                if isinstance(raw_role_reports, dict) and isinstance(raw_role_reports.get(role), dict)
            }
            segment_id = "seg_theme_" + sha256(f"{slot_id}:{amap_id}".encode("utf-8")).hexdigest()[:16]
            poi = CandidateProviderEvidenceService.materialize_poi(
                canonical_candidate,
                local_id="poi_theme_" + sha256(f"{slot_id}:{amap_id}".encode("utf-8")).hexdigest()[:16],
            )
            if report.get(
                "schemaVersion"
            ) == "consumer-candidate-admission-v2" and not ConsumerCandidateAdmissionService.report_matches_poi(
                report, poi
            ):
                continue
            segment = {
                "id": segment_id,
                "kind": "meal" if family in {"meal", "local_food", "food"} else "visit",
                "startTime": start_time or None,
                "endTime": end_time or None,
                "poi": poi,
                "semanticMetadata": {
                    "routeAnchor": True,
                    "portfolioOptional": True,
                    "optionalExperienceFamily": family,
                    "coverageRoles": coverage_roles,
                    "coverageRoleAdmissionReports": coverage_role_reports,
                    "briefId": slot.get("briefId") or candidate.get("briefId"),
                    "creativeBriefId": slot.get("briefId") or candidate.get("briefId"),
                    "poolId": slot.get("poolId") or candidate.get("poolId"),
                    "planningSlotId": slot_id,
                    "sourceGoalId": slot.get("sourceGoalId") or candidate.get("sourceGoalId"),
                    "requirementLevel": slot.get("requirementLevel") or "soft",
                    "consumerAdmissionReport": copy.deepcopy(report),
                },
            }
            day.setdefault("segments", []).append(segment)
            day["segments"].sort(
                key=lambda item: (
                    str(item.get("startTime") or "99:99"),
                    str(item.get("id") or ""),
                )
            )
            pending.remove(slot)
            existing_physical_ids.add(physical_id)
            added_ids.append(segment_id)
        projected["portfolioPendingSlots"] = pending
        after_pairs = {
            (str(item["fromSegmentId"]), str(item["toSegmentId"]))
            for item in ProposalReadinessService.expected_route_pairs(projected)
        }
        retained_pairs = before_pairs & after_pairs
        retained_routes = [
            route
            for route in (
                list(projected.get("routeOptions") or []) + list(projected.get("portfolioRouteEvidence") or [])
            )
            if isinstance(route, dict)
            and (
                str(route.get("fromSegmentId") or ""),
                str(route.get("toSegmentId") or ""),
            )
            in retained_pairs
        ]
        projected["routeOptions"] = retained_routes
        projected["portfolioThemeWalkingEvidence"] = [
            copy.deepcopy(route)
            for route in projected.get("portfolioThemeWalkingEvidence") or []
            if isinstance(route, dict)
            and (
                str(route.get("fromSegmentId") or ""),
                str(route.get("toSegmentId") or ""),
            )
            in retained_pairs
        ]
        projected["portfolioRouteEvidence"] = []
        projected["routeEvidence"] = []
        if added_ids:
            projected["routeEvidenceInvalidationReason"] = "anchor_sequence_changed"
        projected.update(ProposalReadinessService.density_targets(projected))
        return projected, added_ids

    @staticmethod
    def _add_minutes(value: str, minutes: int) -> str:
        try:
            hour, minute = value.split(":", 1)
            total = int(hour) * 60 + int(minute) + max(1, int(minutes))
        except (TypeError, ValueError):
            return ""
        return f"{(total // 60) % 24:02d}:{total % 60:02d}"

    @staticmethod
    def _invoke_bounded(
        callback: Callable[[dict[str, Any]], dict[str, Any]],
        candidate: dict[str, Any],
        *,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        if timeout_seconds <= 0:
            raise FutureTimeoutError()
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="theme-completion")
        context = copy_context()
        future = executor.submit(context.run, callback, candidate)
        try:
            return future.result(timeout=timeout_seconds)
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    @staticmethod
    def _fair_candidate_order(
        meal_candidates_by_slot: dict[str, list[dict[str, Any]]],
        area_candidates: list[dict[str, Any]],
    ) -> list[tuple[str, dict[str, Any]]]:
        slots = [(slot_id, list(candidates)) for slot_id, candidates in sorted(meal_candidates_by_slot.items())]
        ordered: list[tuple[str, dict[str, Any]]] = []
        round_index = 0
        while any(round_index < len(candidates) for _slot, candidates in slots):
            for slot_id, candidates in slots:
                if round_index < len(candidates):
                    ordered.append((slot_id, candidates[round_index]))
            round_index += 1
        ordered.extend(("", candidate) for candidate in area_candidates)
        return ordered

    @staticmethod
    def replacement_diff(
        *,
        before: Iterable[dict[str, Any]],
        after: Iterable[dict[str, Any]],
    ) -> dict[str, Any]:
        before_by_segment = {
            str(item.get("segmentId") or ""): item
            for item in before
            if isinstance(item, dict) and str(item.get("segmentId") or "")
        }
        replacements: list[dict[str, Any]] = []
        blocked_replacements: list[dict[str, Any]] = []
        for item in after:
            if not isinstance(item, dict):
                continue
            segment_id = str(item.get("segmentId") or "")
            previous = before_by_segment.get(segment_id)
            if not previous:
                continue
            before_id = str(previous.get("amapId") or "")
            after_id = str(item.get("amapId") or "")
            if not (
                str(previous.get("requirementLevel") or "") in {"hard", "required"}
                and before_id
                and after_id
                and before_id != after_id
            ):
                continue

            reason_codes: list[str] = []
            if previous.get("userLocked") is True:
                reason_codes.append("replacement_original_user_locked")

            before_occurrence = str(previous.get("intentOccurrenceId") or previous.get("occurrenceId") or "").strip()
            after_occurrence = str(item.get("intentOccurrenceId") or item.get("occurrenceId") or "").strip()
            same_occurrence = item.get("sameIntentOccurrence") is True or (
                bool(before_occurrence) and before_occurrence == after_occurrence
            )
            if not same_occurrence:
                reason_codes.append("replacement_intent_occurrence_mismatch")

            if str(item.get("replacementAdmission") or "").strip().lower() != "admitted":
                reason_codes.append("replacement_admission_failed")

            before_evidence = CreativeThemeCompletionService._safe_number(previous.get("evidenceScore"))
            after_evidence = CreativeThemeCompletionService._safe_number(item.get("evidenceScore"))
            if before_evidence is None or after_evidence is None or after_evidence < before_evidence:
                reason_codes.append("replacement_evidence_regressed")

            before_coverage = CreativeThemeCompletionService._safe_number(previous.get("hardCoverageCount"))
            after_coverage = CreativeThemeCompletionService._safe_number(item.get("hardCoverageCount"))
            if before_coverage is None or after_coverage is None or after_coverage < before_coverage:
                reason_codes.append("replacement_hard_coverage_regressed")

            if item.get("scheduleFeasible") is not True:
                reason_codes.append("replacement_schedule_infeasible")

            material_improvement = (
                (CreativeThemeCompletionService._safe_number(item.get("travelMinutesSaved")) or 0.0) >= 15.0
                or (CreativeThemeCompletionService._safe_number(item.get("transfersReduced")) or 0.0) >= 2.0
                or (CreativeThemeCompletionService._safe_number(item.get("continuousWalkingKmReduced")) or 0.0) >= 1.0
                or item.get("enablesThemeClosure") is True
            )
            if not material_improvement:
                reason_codes.append("replacement_material_improvement_missing")

            replacement = {
                "segmentId": segment_id,
                "beforeAmapId": before_id,
                "afterAmapId": after_id,
            }
            if reason_codes:
                blocked_replacements.append({**replacement, "reasonCodes": reason_codes})
                continue
            replacements.append(replacement)

        return {
            "requiresConfirmation": bool(replacements),
            "hardReplacements": replacements,
            "blockedHardReplacements": blocked_replacements,
            "writeAuthority": "none",
            "versionWriteCount": 0,
            "patchWriteCount": 0,
        }

    @staticmethod
    def reject_hard_replacements(
        *,
        before_snapshot: dict[str, Any],
        proposed_snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        """Restore original hard anchors while retaining compatible additions."""

        from src.services.proposal_readiness_service import ProposalReadinessService

        rejected = copy.deepcopy(proposed_snapshot)
        before_by_segment = {
            str(segment.get("id") or ""): copy.deepcopy(segment)
            for day in before_snapshot.get("days") or []
            if isinstance(day, dict)
            for segment in day.get("segments") or []
            if isinstance(segment, dict)
            and str(segment.get("id") or "")
            and str((segment.get("semanticMetadata") or {}).get("requirementLevel") or "") in {"hard", "required"}
        }
        replacement_rejected = False
        for day in rejected.get("days") or []:
            if not isinstance(day, dict):
                continue
            restored_segments: list[dict[str, Any]] = []
            for segment in day.get("segments") or []:
                if not isinstance(segment, dict):
                    continue
                segment_id = str(segment.get("id") or "")
                original = before_by_segment.get(segment_id)
                if original is None:
                    restored_segments.append(segment)
                    continue
                before_amap_id = str((original.get("poi") or {}).get("amapId") or "")
                proposed_amap_id = str((segment.get("poi") or {}).get("amapId") or "")
                if before_amap_id and proposed_amap_id and before_amap_id != proposed_amap_id:
                    restored_segments.append(copy.deepcopy(original))
                    replacement_rejected = True
                else:
                    restored_segments.append(segment)
            day["segments"] = restored_segments

        if replacement_rejected:
            rejected["routeOptions"] = []
            rejected["portfolioRouteEvidence"] = []
            rejected["routeEvidence"] = []
            rejected["routeEvidenceInvalidationReason"] = "hard_replacement_rejected"
            rejected["portfolioThemeWalkingEvidence"] = []
            rejected.update(ProposalReadinessService.density_targets(rejected))
        rejected["versionWriteCount"] = 0
        rejected["patchWriteCount"] = 0
        return rejected

    @staticmethod
    def _safe_number(value: Any) -> Optional[float]:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        try:
            return float(str(value).strip())
        except (TypeError, ValueError):
            return None

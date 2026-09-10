import copy
import json
import math
import re
import sqlite3
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Optional
from uuid import uuid4

from fastapi import HTTPException

from src.api.schemas.itinerary_patches import (
    ItineraryPatchOperation,
    ItineraryPatchResponse,
    ItineraryPatchSummaryResponse,
    ItineraryVersionResponse,
)
from src.api.schemas.maps import MapPoiResponse
from src.models.poi import POI
from src.models.route_option import RouteOption, normalize_route_mode, route_evidence_status
from src.services.agent_verifier_service import AgentVerifierService
from src.services.itinerary_service import ItineraryService
from src.services.intent_candidate_semantic_policy import IntentCandidateSemanticPolicy
from src.services.agent_run_control import assert_session_run_active, begin_session_run_write
from src.services.itinerary_schedule_service import ItineraryScheduleService
from src.services.itinerary_snapshot_service import ItinerarySnapshotService
from src.services.timeline_write_lease import timeline_write_lease
from src.services.map_poi_service import AMAP_PLACE_SOURCE, MapPoiService
from src.services.meal_diversity_policy import MealDiversityPolicy
from src.services.meal_grounding_policy import MealGroundingPolicy
from src.services.portfolio_pending_slot_service import PortfolioPendingSlotService
from src.services.portfolio_pending_slot_schedule_service import (
    PortfolioPendingSlotScheduleService,
)
from src.services.provider_route_insertion_service import ProviderRouteInsertionService
from src.services.route_insertion_scorer import RouteInsertionScorer
from src.services.route_service import AMAP_ROUTE_SOURCE, ROUTE_CACHE_TTL_SECONDS
from src.services.route_optimization_service import RouteOptimizationResult, RouteOptimizationService
from src.services.versioned_write_guard_service import (
    ensure_base_version_current,
    patch_validation_error_detail,
)
from src.services.visit_duration_policy import VisitDurationPolicy


SKELETON_POI_SOURCE = "itinerary-skeleton"
NEW_DAY_TARGET_ID = "__new_day__"
# Kept as audit labels only.  Route-matrix enforcement is determined from the
# operations' resulting topology, never from this caller-controlled label.
ROUTE_MATRIX_GATED_SOURCE_TYPES = {"agent", "user_timeline_mutation"}
MEAL_CANDIDATE_TYPE_RE = re.compile(
    r"(餐饮服务|中餐厅|西餐厅|外国餐厅|餐厅|饭店|食府|酒楼|小吃|快餐|咖啡|茶馆|甜品|面包|火锅|烧烤|料理|"
    r"restaurant|dining|cafe|café|coffee)",
    re.IGNORECASE,
)
MEAL_CANDIDATE_REJECT_RE = re.compile(
    r"(酒店|宾馆|旅馆|民宿|公寓|住宿|公司|写字楼|停车场|住宅|小区|房地产|售票|票务|服务中心|管理处|办公室|"
    r"超市|便利店|商店|专卖店|学校|医院|银行|水站|卫生间|厕所|公交站|地铁站)"
)


class ItineraryPatchService:
    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.meal_diversity_policy = MealDiversityPolicy()
        self.route_insertion_scorer = RouteInsertionScorer()
        self.provider_route_insertion_service = ProviderRouteInsertionService()
        self.intent_candidate_semantic_policy = IntentCandidateSemanticPolicy()

    def rebind_public_amap_poi_identities(
        self,
        plan_id: str,
        operations: list[ItineraryPatchOperation],
        *,
        planning_context: Optional[dict] = None,
        map_poi_service: Optional[MapPoiService] = None,
    ) -> list[ItineraryPatchOperation]:
        """Bind public patch POIs to server-observed AMap facts before any write.

        A public caller may choose an AMap ID, but cannot attest that the name
        and coordinates belong to that ID.  Pending-candidate selections are
        rebound to the already persisted provider result; other direct map
        edits are checked against the AMap detail endpoint.  The method is
        deliberately read-only so provider failure cannot create a rejected
        patch row, version, route, or partial timeline mutation.
        """

        plan_exists = self.db.execute(
            "SELECT 1 FROM itinerary_plans WHERE id = ?",
            (plan_id,),
        ).fetchone()
        if plan_exists is None:
            raise HTTPException(status_code=404, detail="Conversation session for itinerary plan not found")
        if any(operation.op == "replace_itinerary" for operation in operations):
            raise HTTPException(
                status_code=400,
                detail={
                    "validationErrors": [
                        "replace_itinerary is server-only; public callers must use bounded patch operations"
                    ]
                },
            )

        candidate_ids = self._pending_candidate_ids(planning_context, operations)
        candidate_sources: dict[str, MapPoiResponse] = {}
        if candidate_ids:
            session = self.db.execute(
                "SELECT id FROM conversation_sessions WHERE active_plan_id = ?",
                (plan_id,),
            ).fetchone()
            if session is None:
                raise HTTPException(
                    status_code=409,
                    detail="Pending POI candidate confirmation requires an existing conversation session",
                )
            candidate_sources = self._public_pending_candidate_sources(str(session["id"]), candidate_ids)
        detail_service = map_poi_service or MapPoiService()
        rebound: list[ItineraryPatchOperation] = []
        for operation in operations:
            requested = operation.amap_poi
            if requested is None:
                rebound.append(operation)
                continue
            requested_id = str(requested.id or "").strip()
            if not re.fullmatch(r"B[0-9A-Z]{8,31}", requested_id):
                raise HTTPException(status_code=400, detail="Public patch AMap POI ID is not canonical")
            canonical = candidate_sources.get(requested_id)
            if canonical is None:
                canonical = detail_service.detail(requested_id)
            self._assert_public_amap_identity_matches(requested, canonical)
            rebound.append(operation.model_copy(update={"amap_poi": canonical}))
        return rebound

    def _public_pending_candidate_sources(self, session_id: str, candidate_ids: list[str]) -> dict[str, MapPoiResponse]:
        sources: dict[str, MapPoiResponse] = {}
        for candidate_id in candidate_ids:
            row = self.db.execute(
                """
                SELECT candidates_json
                FROM amap_poi_candidates
                WHERE id = ? AND session_id = ? AND status IN ('pending', 'selected')
                """,
                (candidate_id, session_id),
            ).fetchone()
            if row is None:
                continue
            try:
                candidates = json.loads(row["candidates_json"] or "[]")
            except json.JSONDecodeError:
                candidates = []
            for payload in candidates:
                if not isinstance(payload, dict):
                    continue
                try:
                    candidate = MapPoiResponse.model_validate(payload)
                except (TypeError, ValueError):
                    continue
                candidate_id_value = str(candidate.id or "").strip()
                if candidate_id_value:
                    sources[candidate_id_value] = candidate
        return sources

    @staticmethod
    def _assert_public_amap_identity_matches(requested: MapPoiResponse, canonical: MapPoiResponse) -> None:
        if not re.fullmatch(r"B[0-9A-Z]{8,31}", str(requested.id or "").strip()):
            raise HTTPException(status_code=400, detail="Public patch AMap POI ID is not canonical")
        if str(requested.source or "").strip() != AMAP_PLACE_SOURCE:
            raise HTTPException(status_code=400, detail="Public patch AMap POI source is invalid")
        if str(requested.id or "").strip() != str(canonical.id or "").strip():
            raise HTTPException(status_code=400, detail="Public patch AMap POI ID does not match provider detail")
        if str(requested.name or "").strip() != str(canonical.name or "").strip():
            raise HTTPException(status_code=400, detail="Public patch AMap POI name does not match provider detail")
        for field_name in ("longitude", "latitude"):
            if abs(float(getattr(requested, field_name)) - float(getattr(canonical, field_name))) > 1e-6:
                raise HTTPException(
                    status_code=400,
                    detail=f"Public patch AMap POI {field_name} does not match provider detail",
                )

    def apply_patch(
        self,
        plan_id: str,
        operations: list[ItineraryPatchOperation],
        source_type: str = "manual",
        base_version_id: Optional[str] = None,
        source_turn_id: Optional[str] = None,
        preference_summary: Optional[str] = None,
        planning_context: Optional[dict] = None,
        server_route_decision_contract: Optional[dict] = None,
        preserve_server_snapshot_schedule: bool = False,
        allow_server_sealed_zero_target_days: bool = False,
    ) -> ItineraryPatchResponse:
        with timeline_write_lease(plan_id):
            return self._apply_patch_locked(
                plan_id,
                operations,
                source_type=source_type,
                base_version_id=base_version_id,
                source_turn_id=source_turn_id,
                preference_summary=preference_summary,
                planning_context=planning_context,
                server_route_decision_contract=server_route_decision_contract,
                preserve_server_snapshot_schedule=preserve_server_snapshot_schedule,
                allow_server_sealed_zero_target_days=allow_server_sealed_zero_target_days,
            )

    def _apply_patch_locked(
        self,
        plan_id: str,
        operations: list[ItineraryPatchOperation],
        source_type: str = "manual",
        base_version_id: Optional[str] = None,
        source_turn_id: Optional[str] = None,
        preference_summary: Optional[str] = None,
        planning_context: Optional[dict] = None,
        server_route_decision_contract: Optional[dict] = None,
        preserve_server_snapshot_schedule: bool = False,
        allow_server_sealed_zero_target_days: bool = False,
    ) -> ItineraryPatchResponse:
        session = self._session_for_plan(plan_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Conversation session for itinerary plan not found")

        def begin_agent_write() -> None:
            # Rejected patch audit rows are writes too. Every accepted or
            # rejected Agent patch must cross the same cancellation fence
            # immediately before its first database mutation.
            if source_type == "agent":
                begin_session_run_write(str(session["id"]))

        planning_context = dict(planning_context or {})
        # This capability is minted from the trusted server call path.  A
        # client-projected planning context must never opt itself into the
        # authoritative user-clock overlay used by timeline mutations.
        planning_context["_serverUserTimelineMutation"] = source_type == "user_timeline_mutation"
        simple_open_non_blocking_routes = self._authorized_simple_open_route_policy(
            session_id=str(session["id"]),
            source_type=source_type,
            source_turn_id=source_turn_id,
            base_version_id=base_version_id,
            planning_context=planning_context,
        )
        # ``planning_context`` can contain client-projected fields.  Always
        # overwrite the private capability and mint it only from the dedicated
        # server call parameter plus the already verified Agent/Simple route.
        # A browser cannot opt itself out of duration normalization by copying
        # the public-looking Simple Direction flags.
        planning_context["_serverPreserveSavedSimpleDirectionSchedule"] = bool(
            preserve_server_snapshot_schedule
            and source_type == "agent"
            and simple_open_non_blocking_routes
            and planning_context.get("simpleDirectionCommit") is True
            and planning_context.get("simpleDirectionSavedSnapshotRestore") is True
        )
        server_sealed_zero_target_days_authorized = bool(
            allow_server_sealed_zero_target_days
            and source_type == "agent"
            and simple_open_non_blocking_routes
            and planning_context.get("simpleDirectionCommit") is True
        )
        replay_candidate_ids = self._pending_candidate_ids(planning_context, operations)
        replay_candidate_id = replay_candidate_ids[0] if len(replay_candidate_ids) == 1 else None
        replay_response = self._selected_pending_candidate_replay_response(
            session,
            plan_id,
            operations,
            replay_candidate_id,
            base_version_id=base_version_id,
        )
        if replay_response is not None:
            return replay_response
        ensure_base_version_current(base_version_id, session["active_version_id"])

        route_topology_changed = self._requires_provider_matrix_preflight(operations)
        simple_open_route_assignment_invalidated = self._invalidates_simple_open_route_assignment(operations)
        # Simple Open relaxes route *availability*, never the route-decision
        # contract.  Normalize the same server-owned policy before either the
        # strict matrix path or the non-blocking Provider refresh path.
        if route_topology_changed:
            contract_error = self._inject_server_route_decision_contract(
                planning_context,
                source_type=source_type,
                server_route_decision_contract=server_route_decision_contract,
                persisted_route_decision_contract=(
                    self._persisted_route_decision_contract(str(session["active_version_id"] or ""), str(session["id"]))
                ),
            )
            if contract_error:
                validation_errors = [contract_error]
            else:
                validation_errors = []
        else:
            validation_errors = []

        timeline_mutation = (
            planning_context.get("timelineMutation")
            if isinstance(planning_context, dict) and isinstance(planning_context.get("timelineMutation"), dict)
            else {}
        )
        mutation_id = str(timeline_mutation.get("mutationId") or "") or None
        operations = self._normalize_visit_durations(operations, planning_context)
        planning_context["_serverExplicitDurationOverrides"] = {
            str(operation.segment_id or ""): int(operation.duration_minutes or 0)
            for operation in operations
            if operation.op == "replace_segment_duration" and operation.segment_id and int(operation.duration_minutes or 0) > 0
        }
        operations = self._assign_route_preflight_ids(
            operations, source_type=source_type, route_topology_changed=route_topology_changed
        )
        operations = self._materialize_agent_snapshot_route_anchors(
            plan_id,
            operations,
            source_type=source_type,
        )
        pending_candidate_ids = self._pending_candidate_ids(planning_context, operations)
        pending_candidate_id = pending_candidate_ids[0] if len(pending_candidate_ids) == 1 else None
        if pending_candidate_id:
            operations = self._bind_pending_candidate_id(operations, pending_candidate_id)
        validation_errors.extend(
            self._validate(
                plan_id,
                operations,
                planning_context=planning_context,
                allow_server_sealed_zero_target_days=server_sealed_zero_target_days_authorized,
            )
        )
        if len(pending_candidate_ids) > 1:
            validation_errors.append("Only one pending POI candidate can be confirmed per itinerary patch")
        elif pending_candidate_id:
            validation_errors.extend(self._validate_pending_candidate(session["id"], pending_candidate_id, operations))
        if source_type == "agent":
            validation_errors.extend(self._user_confirmed_segment_protection_errors(plan_id, operations))
        if route_topology_changed and not simple_open_non_blocking_routes and not validation_errors:
            validation_errors.extend(
                self._ensure_agent_route_matrix_proofs(
                    plan_id,
                    operations,
                    planning_context,
                    base_version_id=base_version_id,
                    preference_summary=preference_summary,
                )
            )
        if (
            not validation_errors
            and not simple_open_non_blocking_routes
            and (
                route_topology_changed
                or (isinstance(planning_context, dict) and "routeInsertionProofs" in planning_context)
            )
        ):
            validation_errors.extend(
                self._route_insertion_proof_validation_errors(
                    operations,
                    planning_context,
                    base_version_id=base_version_id,
                )
            )
        prepared_expansions: dict[str, dict] = {}
        if not validation_errors:
            try:
                prepared_expansions = self._prepare_poi_candidate_expansions(plan_id, operations)
            except HTTPException as error:
                self.db.rollback()
                message = self._http_error_message(error)
                begin_agent_write()
                patch_id = self._insert_patch(
                    session["id"],
                    plan_id,
                    base_version_id,
                    None,
                    source_type,
                    source_turn_id,
                    operations,
                    "rejected",
                    [message],
                    mutation_id=mutation_id,
                )
                self.db.commit()
                raise HTTPException(
                    status_code=error.status_code, detail=patch_validation_error_detail(patch_id, [message])
                ) from error
        if validation_errors == ["initial_route_matrix_requires_grounded_amap_anchors"] and source_type == "agent":
            # This is a candidate-grounding checkpoint, not an attempted
            # itinerary mutation.  Surface the typed cause before the first
            # database write so the Agent can return candidate_refresh_required
            # with exact version/patch/route deltas of zero.
            raise HTTPException(
                status_code=409,
                detail=patch_validation_error_detail("", validation_errors),
            )
        if validation_errors:
            begin_agent_write()
            patch_id = self._insert_patch(
                session["id"],
                plan_id,
                base_version_id,
                None,
                source_type,
                source_turn_id,
                operations,
                "rejected",
                validation_errors,
                mutation_id=mutation_id,
            )
            self.db.commit()
            raise HTTPException(status_code=400, detail=patch_validation_error_detail(patch_id, validation_errors))

        begin_agent_write()

        pending_claimed = False
        selected_amap_id = self._selected_amap_id(operations) or ""
        try:
            if pending_candidate_id:
                pending_claimed = self._claim_pending_candidate_selection(
                    session["id"], pending_candidate_id, selected_amap_id
                )
            route_scope_before = (
                self._route_scope_before(plan_id, operations) if self._requires_route_refresh(operations) else None
            )
            patch_id = self._insert_patch(
                session["id"],
                plan_id,
                base_version_id,
                None,
                source_type,
                source_turn_id,
                operations,
                "accepted",
                [],
                mutation_id=mutation_id,
            )
            self._persist_budget_tier(plan_id, planning_context)
            self._apply_operations(
                plan_id,
                operations,
                prepared_expansions=prepared_expansions,
                source_type=source_type,
                planning_context=planning_context,
            )
            if not simple_open_non_blocking_routes:
                self._persist_provider_matrix_routes(plan_id, planning_context)
            self._inject_timeline_mutation_failure(planning_context, "after core patch")
            self._assert_patch_deadline(planning_context)
            self._invalidate_stale_ticket_results(plan_id, self._ticket_invalidation_segment_ids(operations))
            ticket_segment_ids = self._ticket_refresh_segment_ids(operations)
            if ticket_segment_ids:
                ItineraryService(self.db).refresh_ticket_results_for_segments(plan_id, ticket_segment_ids)
            route_pairs = self._route_pairs_after(plan_id, route_scope_before)
            if simple_open_non_blocking_routes:
                # The strict pair calculator intentionally ignores non-legacy
                # semantic kinds such as campus/night_view.  Let the explicitly
                # authorized RouteService call derive all adjacent semantic
                # anchors for this plan instead of globally weakening strict
                # pair rules.
                route_pairs = None
            schedule_route_pairs = route_pairs
            matrix_pairs = (
                set() if simple_open_non_blocking_routes else self._provider_matrix_route_pairs(planning_context)
            )
            if matrix_pairs:
                route_pairs = matrix_pairs.union(route_pairs or set())
            self.db.commit()
            route_warnings = []
            schedule_updated_count = 0
            simple_open_route_status = "unknown"
            simple_open_route_write_delta = 0
            route_count_before_refresh = int(
                self.db.execute("SELECT COUNT(*) FROM route_options WHERE plan_id = ?", (plan_id,)).fetchone()[0]
            )
            route_refresh_policy = self._route_refresh_policy(planning_context, source_type)
            if simple_open_non_blocking_routes:
                # Reuse the bounded adjacent-pair refresher inside this same
                # canonical write.  Its exception path deletes incomplete
                # route rows and returns a warning, so route Provider failure
                # cannot erase the usable itinerary or create another version.
                route_refresh_policy = "touched_pairs_only"
            if self._route_insertion_proofs(planning_context) and not simple_open_non_blocking_routes:
                # The guarded preflight has already fetched every decisive
                # Provider leg and _persist_provider_matrix_routes stored the
                # same selected routes.  A second Provider call here could
                # fail after the business patch was committed and erase that
                # verified coverage, so route enrichment is deliberately
                # skipped for this write.
                route_refresh_policy = "skip"
            if self._requires_route_refresh(operations) and route_refresh_policy != "skip":
                self._assert_patch_deadline(planning_context)
                preferred_mode = self._preferred_transport_mode(operations, planning_context)
                itinerary_service = ItineraryService(self.db)
                if route_refresh_policy == "touched_pairs_only":
                    try:
                        route_warnings = itinerary_service.refresh_routes(
                            plan_id,
                            preferred_mode=preferred_mode,
                            route_pairs=route_pairs,
                            allow_semantic_route_anchors=simple_open_non_blocking_routes,
                        )
                        self.db.commit()
                    except Exception as error:
                        self.db.rollback()
                        itinerary_service._delete_routes(plan_id, route_pairs)
                        self.db.commit()
                        route_warnings = [f"地图/路线工具失败；已移除受影响区间的旧路线，未保留虚假交通时长：{error}"]
                else:
                    compact_route_modes = bool(
                        isinstance(planning_context, dict) and planning_context.get("compactRouteModes")
                    )
                    if compact_route_modes:
                        try:
                            if isinstance(planning_context, dict) and planning_context.get("portfolioCommit"):
                                # Proposal preflight queries the preferred mode for every
                                # adjacent pair, then asks compact fallbacks only for meal
                                # detours. Persist the same bounded route set so the shared
                                # schedule projection remains identical after commit.
                                route_warnings = itinerary_service.refresh_routes(
                                    plan_id,
                                    preferred_mode=preferred_mode,
                                    route_pairs=route_pairs,
                                    preferred_mode_only=True,
                                )
                                meal_pairs = self._portfolio_meal_route_pairs(plan_id, route_pairs)
                                if meal_pairs:
                                    route_warnings.extend(
                                        itinerary_service.refresh_routes(
                                            plan_id,
                                            preferred_mode=preferred_mode,
                                            route_pairs=meal_pairs,
                                            include_compact_fallbacks=True,
                                        )
                                    )
                            else:
                                route_warnings = itinerary_service.refresh_routes(
                                    plan_id,
                                    preferred_mode=preferred_mode,
                                    route_pairs=route_pairs,
                                    include_compact_fallbacks=True,
                                )
                            self.db.commit()
                        except Exception as error:
                            self.db.rollback()
                            itinerary_service._delete_routes(plan_id, route_pairs)
                            self.db.commit()
                            route_warnings = [
                                f"地图/路线工具失败；已移除受影响区间的旧路线，未保留虚假交通时长：{error}"
                            ]
                            if isinstance(planning_context, dict) and planning_context.get("portfolioCommit"):
                                raise HTTPException(
                                    status_code=409,
                                    detail={
                                        "code": "plan_proposal_route_quality_failed",
                                        "message": "所选方案的正式路线刷新失败，未创建正式行程。",
                                        "details": {
                                            "routeQualityIssues": [
                                                {"code": "route_refresh_failed", "message": str(error)}
                                            ],
                                            "recommendedNextActions": [
                                                "retry_route_verification",
                                                "open_map_selection",
                                            ],
                                        },
                                    },
                                )
                    else:
                        refresh_kwargs = {
                            "preference_summary": preference_summary,
                            "planning_context": planning_context,
                            "preferred_mode": preferred_mode,
                            "commit_between_tools": True,
                        }
                        if route_pairs is not None:
                            refresh_kwargs["route_pairs"] = route_pairs
                        route_warnings = itinerary_service.refresh_planning_tools(plan_id, **refresh_kwargs)
            if simple_open_non_blocking_routes:
                route_count_after_refresh = int(
                    self.db.execute("SELECT COUNT(*) FROM route_options WHERE plan_id = ?", (plan_id,)).fetchone()[0]
                )
                simple_open_route_write_delta = max(route_count_after_refresh - route_count_before_refresh, 0)
                if route_count_after_refresh > 0:
                    simple_open_route_status = "partial" if route_warnings else "ready"
                elif any("失败" in warning or "未配置" in warning for warning in route_warnings):
                    simple_open_route_status = "provider_failed"
            self._inject_timeline_mutation_failure(planning_context, "after route refresh")
            route_matrix_requires_projection = any(
                proof.get("scheduleProjectionRequired") is True
                for proof in self._route_insertion_proofs(planning_context)
            )
            preserve_saved_simple_direction_schedule = bool(
                isinstance(planning_context, dict)
                and planning_context.get("_serverPreserveSavedSimpleDirectionSchedule") is True
            )
            should_recompute_schedule = self._requires_schedule_recompute(operations) and not (
                (source_type == "user_timeline_mutation" and not route_matrix_requires_projection)
                or preserve_saved_simple_direction_schedule
            )
            if should_recompute_schedule:
                self._assert_patch_deadline(planning_context)
                schedule_updated_count = ItineraryScheduleService(self.db).recompute_plan_schedule(
                    plan_id,
                    route_pairs=schedule_route_pairs,
                )
            self._inject_timeline_mutation_failure(planning_context, "after schedule recompute")
            snapshot_service = ItinerarySnapshotService(self.db)
            self._assert_patch_deadline(planning_context)
            snapshot = snapshot_service.capture_snapshot(plan_id)
            full_itinerary_replacement = any(
                operation.op == "replace_itinerary" and isinstance(operation.full_itinerary, dict)
                for operation in operations
            )
            if base_version_id and not full_itinerary_replacement:
                # Live itinerary tables contain the editable days/segments but
                # not planner-only proposal state.  A local patch starts from
                # those live tables, so carry the active version envelope before
                # saving the next version.  Otherwise an innocuous time edit
                # silently drops pending slots, partial/adoption lineage, and
                # the creative brief even though the operation never touched
                # any of them.
                base_metadata_row = self.db.execute(
                    "SELECT snapshot_json FROM itinerary_versions WHERE id = ? AND session_id = ? AND plan_id = ?",
                    (base_version_id, session["id"], plan_id),
                ).fetchone()
                if base_metadata_row is None:
                    raise HTTPException(status_code=409, detail={"code": "active_version_snapshot_missing"})
                try:
                    base_metadata_snapshot = json.loads(base_metadata_row["snapshot_json"] or "{}")
                except (TypeError, ValueError, json.JSONDecodeError) as error:
                    raise HTTPException(
                        status_code=409,
                        detail={"code": "active_version_snapshot_invalid"},
                    ) from error
                snapshot_service.copy_version_metadata(base_metadata_snapshot, snapshot)
                if simple_open_route_assignment_invalidated:
                    # Frozen Simple Open route evidence is bound to the exact
                    # ordered AMap pair set. A POI/order topology mutation must
                    # invalidate it; time-only edits may safely preserve it.
                    snapshot.pop("simpleOpenRouteAssignment", None)
            # Route Provider failure is non-blocking for Simple Open, but the
            # server-authored route policy is not optional.  Carry it into
            # every formal version so later manual edits and route actions do
            # not lose the authority that was confirmed before generation.
            self._carry_forward_route_decision_contract(
                snapshot,
                planning_context=planning_context,
                active_version_id=str(session["active_version_id"] or ""),
                session_id=str(session["id"]),
                route_topology_changed=route_topology_changed,
            )
            if simple_open_non_blocking_routes:
                snapshot["simpleOpenRouteStatus"] = simple_open_route_status
                snapshot["simpleOpenExecutionProfile"] = "simple_open_v1"
                snapshot["simpleOpenExecutionRoute"] = "simple_open_initial_pipeline"
            pending_slot_selection = (
                planning_context.get("pendingSlotSelection")
                if isinstance(planning_context, dict) and isinstance(planning_context.get("pendingSlotSelection"), dict)
                else None
            )
            if pending_slot_selection is not None and base_version_id:
                base_row = self.db.execute(
                    "SELECT snapshot_json FROM itinerary_versions WHERE id = ? AND session_id = ?",
                    (base_version_id, session["id"]),
                ).fetchone()
                if base_row is None:
                    raise HTTPException(status_code=409, detail={"code": "active_version_snapshot_missing"})
                base_snapshot = json.loads(base_row["snapshot_json"] or "{}")
                for key in ItinerarySnapshotService.VERSION_METADATA_KEYS:
                    if key in base_snapshot:
                        snapshot[key] = base_snapshot[key]
                snapshot = PortfolioPendingSlotScheduleService().project(snapshot)
                snapshot = PortfolioPendingSlotService.reconcile(snapshot)
            if isinstance(planning_context, dict) and (
                planning_context.get("portfolioCommit")
                or planning_context.get("portfolioPartialTimeline")
                or planning_context.get("simpleDirectionCommit")
            ):
                selected_snapshot = next(
                    (
                        operation.full_itinerary
                        for operation in operations
                        if operation.op == "replace_itinerary" and isinstance(operation.full_itinerary, dict)
                    ),
                    None,
                )
                if selected_snapshot is not None:
                    for key in ItinerarySnapshotService.VERSION_METADATA_KEYS:
                        if key in selected_snapshot:
                            snapshot[key] = selected_snapshot[key]
                partial_selection_context = (
                    planning_context.get("portfolioSelectionContext")
                    if planning_context.get("portfolioPartialTimeline")
                    and isinstance(planning_context.get("portfolioSelectionContext"), dict)
                    else None
                )
                if partial_selection_context is not None:
                    snapshot["portfolioSelectionContext"] = copy.deepcopy(partial_selection_context)
            hard_constraints = self._hard_constraints_for_snapshot(planning_context)
            if hard_constraints:
                snapshot["hardConstraints"] = hard_constraints
            route_insertion_proofs = self._route_insertion_proofs(planning_context)
            if route_insertion_proofs:
                # Keep the exact Provider preflight alongside the version that
                # is about to become active.  AgentVerifierService compares its
                # candidate legs with the selected routes materialized from the
                # same matrix, so there is no post-write Provider retry window.
                snapshot["routeInsertionProofs"] = copy.deepcopy(route_insertion_proofs)
            if isinstance(planning_context, dict) and isinstance(
                planning_context.get("routeMatrixExpectedPairs"), list
            ):
                snapshot["routeMatrixExpectedPairs"] = copy.deepcopy(planning_context["routeMatrixExpectedPairs"])
            self._assert_patch_deadline(planning_context)
            version = snapshot_service.save_version(
                session["id"],
                plan_id,
                source_type,
                snapshot=snapshot,
                source_turn_id=source_turn_id,
                source_patch_id=patch_id,
            )
            self._inject_timeline_mutation_failure(planning_context, "after version save")
            self.db.execute(
                "UPDATE itinerary_patches SET result_version_id = ? WHERE id = ?",
                (version.id, patch_id),
            )
            if pending_candidate_id:
                self._mark_pending_candidate_selected(
                    session["id"],
                    pending_candidate_id,
                    selected_amap_id,
                )
            self.db.commit()
        except Exception:
            self.db.rollback()
            if pending_claimed:
                self._rollback_pending_candidate_claim(session["id"], pending_candidate_id or "", selected_amap_id)
                self.db.commit()
            raise
        itinerary = ItineraryService(self.db).get_plan(plan_id)
        if route_warnings:
            itinerary.route_warnings.extend(route_warnings)
        return ItineraryPatchResponse(
            itinerary=itinerary,
            patch=ItineraryPatchSummaryResponse(
                id=patch_id,
                validation_status="accepted",
                metadata={
                    "schedulePolicy": "computed_from_selected_routes",
                    "scheduleUpdatedCount": schedule_updated_count,
                    "routeRefreshScope": {
                        "policy": route_refresh_policy,
                        "pairs": [list(pair) for pair in sorted(route_pairs or set())],
                        "status": "partial"
                        if route_warnings and any("地图/路线工具失败" in item for item in route_warnings)
                        else "completed",
                    },
                    "simpleOpenRouteStatus": simple_open_route_status if simple_open_non_blocking_routes else None,
                    "routeWriteDelta": simple_open_route_write_delta,
                },
            ),
            version=version,
            validation_errors=[],
            pending_poi_candidates=self._pending_candidates_payload(session["id"]),
        )

    def _authorized_simple_open_route_policy(
        self,
        *,
        session_id: str,
        source_type: str,
        source_turn_id: Optional[str],
        base_version_id: Optional[str],
        planning_context: dict,
    ) -> bool:
        """Allow non-blocking initial routes only for a server-routed turn.

        ``planning_context`` also contains client supplied context, so its flag
        is intentionally insufficient.  The assistant turn is persisted by
        the coordinator after it chooses the executor route; checking that
        server-owned record prevents a request payload from downgrading the
        strict profile or disabling route proof validation.
        """
        if source_type not in {"agent", "user_timeline_mutation"}:
            return False
        if base_version_id:
            base_row = self.db.execute(
                "SELECT snapshot_json FROM itinerary_versions WHERE id = ? AND session_id = ?",
                (base_version_id, session_id),
            ).fetchone()
            if base_row is not None:
                try:
                    base_snapshot = json.loads(str(base_row["snapshot_json"] or "{}"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    base_snapshot = {}
                if (
                    isinstance(base_snapshot, dict)
                    and base_snapshot.get("simpleOpenExecutionProfile") == "simple_open_v1"
                    and base_snapshot.get("simpleOpenExecutionRoute") == "simple_open_initial_pipeline"
                ):
                    return True
        if source_type != "agent":
            return False
        if not source_turn_id or planning_context.get("simpleOpenNonBlockingRoutes") is not True:
            return False
        row = self.db.execute(
            """SELECT agent_request_json FROM conversation_turns
            WHERE id = ? AND session_id = ? AND role = 'assistant'""",
            (source_turn_id, session_id),
        ).fetchone()
        if row is None:
            return False
        try:
            persisted = json.loads(str(row["agent_request_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
        return bool(
            isinstance(persisted, dict)
            and persisted.get("serverExecutionProfile") == "simple_open_v1"
            and persisted.get("actualExecutionRoute") == "simple_open_initial_pipeline"
            and persisted.get("simpleOpenNonBlockingRoutes") is True
        )

    def _skip_optional_tool_refresh(self, planning_context: Optional[dict], source_type: str) -> bool:
        if source_type != "agent" or not isinstance(planning_context, dict):
            return False
        staged = planning_context.get("stagedPlanningPipeline")
        return bool(
            isinstance(staged, dict) and staged.get("enabled") and planning_context.get("skipOptionalToolRefresh")
        )

    def _assert_patch_deadline(self, planning_context: Optional[dict]) -> None:
        session_id = planning_context.get("sessionId") if isinstance(planning_context, dict) else None
        if session_id:
            assert_session_run_active(str(session_id))
        deadline = planning_context.get("runtimeDeadlineMonotonic") if isinstance(planning_context, dict) else None
        if isinstance(deadline, (int, float)) and time.monotonic() >= float(deadline):
            raise HTTPException(
                status_code=408, detail="patch_deadline_exceeded: core patch did not start before deadline"
            )

    @staticmethod
    def _inject_timeline_mutation_failure(planning_context: Optional[dict], stage: str) -> None:
        injector = (
            planning_context.get("timelineMutationFailureInjector") if isinstance(planning_context, dict) else None
        )
        if callable(injector):
            injector(stage)

    def _route_refresh_policy(self, planning_context: Optional[dict], source_type: str) -> str:
        if not isinstance(planning_context, dict):
            return "full"
        policy = planning_context.get("toolRefreshPolicy")
        if isinstance(policy, dict):
            route_policy = str(policy.get("route") or "").strip()
            if route_policy in {"skip", "touched_pairs_only", "full"}:
                return route_policy
            if route_policy in {"touched", "touched_only", "touchedPairsOnly"}:
                return "touched_pairs_only"
        if source_type != "agent":
            return "full"
        # A local Agent edit is a small write, not permission to synchronously
        # enrich every POI in the itinerary.  This also covers tool-loop edits
        # where the model omitted the optional policy object.
        agent_plan = planning_context.get("agentPlan") if isinstance(planning_context, dict) else None
        task_type = (
            str((agent_plan or {}).get("taskType") or planning_context.get("taskType") or "")
            if isinstance(planning_context, dict)
            else ""
        )
        if task_type == "local_modification":
            return "touched_pairs_only"
        if planning_context.get("refreshTouchedRoutes"):
            return "touched_pairs_only"
        return "skip" if self._skip_optional_tool_refresh(planning_context, source_type) else "full"

    @staticmethod
    def _route_insertion_proofs(planning_context: Optional[dict]) -> list[dict]:
        if not isinstance(planning_context, dict):
            return []
        proofs = planning_context.get("routeInsertionProofs")
        if not isinstance(proofs, list):
            return []
        return [copy.deepcopy(item) for item in proofs if isinstance(item, dict)]

    @classmethod
    def _provider_matrix_route_pairs(
        cls,
        planning_context: Optional[dict],
    ) -> set[tuple[str, str]]:
        pairs: set[tuple[str, str]] = set()
        for proof in cls._route_insertion_proofs(planning_context):
            if str(proof.get("status") or "") != "passed":
                continue
            legs = proof.get("legs") if isinstance(proof.get("legs"), dict) else {}
            for key in ("previousToCandidate", "candidateToNext"):
                leg = legs.get(key)
                if not isinstance(leg, dict):
                    continue
                pair = (
                    str(leg.get("fromSegmentId") or ""),
                    str(leg.get("toSegmentId") or ""),
                )
                if all(pair):
                    pairs.add(pair)
        return pairs

    def _assign_route_preflight_ids(
        self,
        operations: list[ItineraryPatchOperation],
        *,
        source_type: str,
        route_topology_changed: bool,
    ) -> list[ItineraryPatchOperation]:
        """Freeze writer identities before Provider routes are queried.

        The official route refresh and the stored proof must refer to the same
        segment IDs.  Generated IDs therefore belong to the guarded patch
        operation, not to the later mutating helper.
        """
        del source_type  # Audit-only; topology determines whether IDs are frozen.
        if not route_topology_changed:
            return operations
        assigned: list[ItineraryPatchOperation] = []
        for operation in operations:
            if operation.op == "add_segment" and operation.amap_poi is not None:
                assigned.append(
                    operation
                    if operation.segment_id
                    else operation.model_copy(update={"segment_id": f"seg_{uuid4().hex[:12]}"})
                )
                continue
            if operation.op != "replace_itinerary" or not isinstance(operation.full_itinerary, dict):
                assigned.append(operation)
                continue
            snapshot = copy.deepcopy(operation.full_itinerary)
            for day in snapshot.get("days") or []:
                if not isinstance(day, dict):
                    continue
                day.setdefault("id", f"day_{uuid4().hex[:12]}")
                for segment in day.get("segments") or []:
                    if not isinstance(segment, dict):
                        continue
                    segment.setdefault("id", f"seg_{uuid4().hex[:12]}")
                    poi = segment.get("poi")
                    if isinstance(poi, dict):
                        poi.setdefault("id", f"poi_{uuid4().hex[:12]}")
            assigned.append(operation.model_copy(update={"full_itinerary": snapshot}))
        return assigned

    def _inject_server_route_decision_contract(
        self,
        planning_context: dict,
        *,
        source_type: str,
        server_route_decision_contract: Optional[dict],
        persisted_route_decision_contract: Optional[dict],
    ) -> Optional[str]:
        """Install one normalized route policy from a server-owned carrier.

        The public patch endpoint always uses the ``manual`` audit source and
        passes no internal carrier.  It therefore cannot promote a contract in
        its JSON body.  Internal Agent writes can use either the explicit
        argument (preferred) or the already server-built request intent
        context; conflicts fail closed instead of selecting the looser value.
        """
        current_candidates: list[tuple[str, Any]] = []
        if server_route_decision_contract is not None:
            current_candidates.append(("internal_argument", server_route_decision_contract))
        if source_type == "agent":
            request_intent = planning_context.get("requestIntentContract")
            if isinstance(request_intent, dict):
                current_candidates.append(("request_intent_contract", request_intent.get("routeDecisionContract")))
            # Compatibility for server-side Agent callers that already lifted
            # the value before reaching this service.  This branch is not
            # reachable from the public endpoint, which hard-codes manual.
            if "routeDecisionContract" in planning_context:
                current_candidates.append(("server_planning_context", planning_context.get("routeDecisionContract")))
        # The active snapshot is a fallback, not another issuer for the current
        # turn.  A newly compiled server-owned Agent contract may legitimately
        # tighten or extend provenance after an explicit route/experience edit.
        # Multiple current-turn carriers must still agree exactly; a public
        # manual request cannot create one and therefore remains bound to the
        # persisted contract.
        candidates = list(current_candidates)
        if not candidates and persisted_route_decision_contract is not None:
            candidates.append(("active_version_snapshot", persisted_route_decision_contract))
        if not candidates:
            return "provider_route_matrix_preflight_failed:route_decision_contract_missing_or_invalid"
        normalized: list[tuple[str, dict[str, Any]]] = []
        for location, candidate in candidates:
            value = self.route_insertion_scorer.normalized_route_decision_contract(candidate)
            if value is None:
                return f"provider_route_matrix_preflight_failed:route_decision_contract_invalid:{location}"
            normalized.append((location, value))
        fingerprints = {value["fingerprint"] for _, value in normalized}
        if len(fingerprints) != 1:
            return "provider_route_matrix_preflight_failed:route_decision_contract_conflict"

        # ``normalized`` is a verifier projection, not the persisted product
        # contract.  The server-owned raw value can carry additional envelope
        # fields (for example schema/status/missing-fields provenance) that
        # later turns need.  Keep that exact verified carrier instead of
        # silently replacing it with the scorer's five-field projection.
        def complete_envelope(candidate: Any) -> bool:
            return bool(
                isinstance(candidate, dict)
                and str(candidate.get("schemaVersion") or "") == "route-decision-contract-v1"
                and str(candidate.get("status") or "") == "ready"
                and isinstance(candidate.get("missingFields"), list)
                and not candidate.get("missingFields")
            )

        selected_index = next(
            (index for index, (_location, candidate) in enumerate(candidates) if complete_envelope(candidate)),
            0,
        )
        planning_context["routeDecisionContract"] = copy.deepcopy(candidates[selected_index][1])
        planning_context["routeDecisionContractSource"] = normalized[selected_index][0]
        return None

    def _persisted_route_decision_contract(
        self,
        version_id: str,
        session_id: str,
    ) -> Optional[dict]:
        """Read the policy emitted by the current server-owned version.

        This is the safe fallback for a user/manual edit: its request body
        never authorizes a policy, but a prior accepted itinerary version may
        already carry one.  An absent or malformed snapshot value remains a
        fail-closed missing-contract error.
        """
        snapshot = self._persisted_version_snapshot(version_id, session_id)
        if snapshot is None:
            return None
        value = snapshot.get("routeDecisionContract") if isinstance(snapshot, dict) else None
        return copy.deepcopy(value) if isinstance(value, dict) else None

    def _persisted_version_snapshot(self, version_id: str, session_id: str) -> Optional[dict[str, Any]]:
        if not version_id or not session_id:
            return None
        row = self.db.execute(
            "SELECT snapshot_json FROM itinerary_versions WHERE id = ? AND session_id = ?",
            (version_id, session_id),
        ).fetchone()
        if row is None:
            return None
        try:
            snapshot = json.loads(row["snapshot_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            return None
        return snapshot if isinstance(snapshot, dict) else None

    def _carry_forward_route_decision_contract(
        self,
        snapshot: dict[str, Any],
        *,
        planning_context: Optional[dict],
        active_version_id: str,
        session_id: str,
        route_topology_changed: bool,
    ) -> None:
        """Keep the last verified server route policy across every version.

        Snapshot capture serializes mutable itinerary tables only.  Without
        this explicit carrier, a harmless title edit silently erased the
        route authority required by the next topology-changing write.
        Non-topology patches never adopt a request-carried value, so public
        clients and model payloads cannot smuggle a replacement policy.
        """
        persisted_snapshot = self._persisted_version_snapshot(active_version_id, session_id)
        persisted_raw = (
            persisted_snapshot.get("routeDecisionContract") if isinstance(persisted_snapshot, dict) else None
        )
        persisted = self.route_insertion_scorer.normalized_route_decision_contract(persisted_raw)
        if persisted_raw is not None and persisted is None:
            raise HTTPException(
                status_code=409,
                detail="active_version_route_decision_contract_invalid",
            )
        current = None
        if route_topology_changed and isinstance(planning_context, dict):
            current_raw = planning_context.get("routeDecisionContract")
            current = self.route_insertion_scorer.normalized_route_decision_contract(current_raw)
            if current is None:
                raise HTTPException(
                    status_code=409,
                    detail="route_decision_contract_missing_or_invalid_before_version_save",
                )
        # Normalization above is validation only.  Persist the exact
        # server-owned raw carrier: a topology-changing write uses the current
        # planning contract, while a non-topology edit preserves the active
        # version byte-for-byte at the JSON-object level.  Reconstructing an
        # envelope here would both erase valid Simple Direction metadata and
        # mutate legacy portable contracts on otherwise harmless edits.
        contract_raw = current_raw if route_topology_changed else persisted_raw
        if isinstance(contract_raw, dict):
            snapshot["routeDecisionContract"] = copy.deepcopy(contract_raw)
        if (
            not route_topology_changed
            and isinstance(persisted_snapshot, dict)
            and self._route_options_snapshot_equal(persisted_snapshot, snapshot)
        ):
            for key in ("routeInsertionProofs", "routeMatrixExpectedPairs"):
                value = persisted_snapshot.get(key)
                if isinstance(value, list):
                    snapshot[key] = copy.deepcopy(value)

    @staticmethod
    def _route_options_snapshot_equal(left: dict[str, Any], right: dict[str, Any]) -> bool:
        def canonical(snapshot: dict[str, Any]) -> str:
            routes = snapshot.get("routeOptions")
            payload = routes if isinstance(routes, list) else []
            return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)

        return canonical(left) == canonical(right)

    def _active_server_route_decision_contract(
        self,
        *,
        plan_id: str,
        active_version_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        """Read the only route policy public route actions may rely on.

        ``planningContext`` is a public request payload on the route endpoints.
        It may carry presentation metadata such as ``routeGroup``, but it can
        never mint or replace the policy used to publish a new active version.
        """

        if not active_version_id:
            raise HTTPException(
                status_code=409,
                detail="active_version_route_decision_contract_missing",
            )
        ownership = self.db.execute(
            """
            SELECT 1
            FROM itinerary_versions
            WHERE id = ? AND session_id = ? AND plan_id = ?
            """,
            (active_version_id, session_id, plan_id),
        ).fetchone()
        if ownership is None:
            raise HTTPException(
                status_code=409,
                detail="active_version_route_decision_contract_missing",
            )
        persisted_snapshot = self._persisted_version_snapshot(active_version_id, session_id)
        if not isinstance(persisted_snapshot, dict):
            raise HTTPException(
                status_code=409,
                detail="active_version_route_decision_contract_missing",
            )
        raw_contract = persisted_snapshot.get("routeDecisionContract")
        contract = self.route_insertion_scorer.normalized_route_decision_contract(raw_contract)
        if contract is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    "active_version_route_decision_contract_invalid"
                    if raw_contract is not None
                    else "active_version_route_decision_contract_missing"
                ),
            )
        # The normalized projection above is validation evidence only.  Keep
        # the exact persisted server envelope available to the next writer so
        # status/schema/missing-fields metadata cannot be erased by an edit.
        return copy.deepcopy(raw_contract)

    def _rebuild_selected_route_matrix_snapshot(
        self,
        plan_id: str,
        snapshot: dict[str, Any],
        *,
        baseline_selected_rows: list[dict[str, Any]],
        route_decision_contract: dict[str, Any],
        base_version_id: Optional[str],
        operation: str,
    ) -> None:
        """Rebuild final proof from the exact selected Provider rows.

        This runs after the tentative route/schedule update, inside the same
        SQLite savepoint, and before the accepted patch or version is inserted.
        Any mismatch therefore rolls the live itinerary back to its prior
        route selection and schedule instead of publishing proof-less state.
        """

        normalized_contract = self.route_insertion_scorer.normalized_route_decision_contract(route_decision_contract)
        if normalized_contract is None:
            raise HTTPException(
                status_code=409,
                detail="active_version_route_decision_contract_invalid",
            )
        expected_pairs = self._snapshot_adjacent_route_matrix_pairs(snapshot)
        current_rows = self._selected_route_rows_snapshot(plan_id)
        current_by_pair = self._selected_route_row_map(
            current_rows,
            expected_pairs,
            stage="candidate",
        )
        baseline_by_pair = self._selected_route_row_map(
            baseline_selected_rows,
            expected_pairs,
            stage="baseline",
        )
        proofs: list[dict[str, Any]] = []
        for pair, expected in expected_pairs.items():
            left, right, leg = self._validated_selected_route_pair_leg(
                plan_id,
                current_by_pair[pair],
                expected=expected,
            )
            _baseline_left, _baseline_right, baseline_leg = self._validated_selected_route_pair_leg(
                plan_id,
                baseline_by_pair[pair],
                expected=expected,
            )
            fits_current_schedule, slack = self._adjacent_leg_time_window(left, right, leg)
            if not fits_current_schedule or slack is None:
                self._raise_public_route_matrix_error("provider_route_current_schedule_infeasible")
            score = self.route_insertion_scorer.score_from_route_matrix(
                previous_to_candidate=leg,
                candidate_to_next=None,
                previous_to_next=baseline_leg,
                detour_tolerance=normalized_contract["detourTolerance"],
                schedule_slack_minutes=slack,
                time_window_feasible=fits_current_schedule,
                mobility_profile=normalized_contract["mobilityProfile"],
            )
            if score is None:
                self._raise_public_route_matrix_error("provider_route_cost_components_missing")
            proof_status = "passed" if score.network_verified and score.detour_level != "unacceptable" else "failed"
            if proof_status != "passed":
                self._raise_public_route_matrix_error(
                    "provider_route_decision_contract_exceeded",
                    validation_errors=[
                        "provider_route_decision_contract_exceeded",
                        f"pair:{pair[0]}->{pair[1]}",
                    ],
                )
            candidate_row = current_by_pair[pair]
            baseline_row = baseline_by_pair[pair]
            proofs.append(
                {
                    "proofType": "adjacent_route_coverage",
                    "operation": operation,
                    "status": proof_status,
                    "failureReason": None,
                    "networkVerified": bool(score.network_verified),
                    "detourLevel": score.detour_level,
                    "generalizedCostDelta": score.generalized_cost_delta,
                    "detourRatio": score.detour_ratio,
                    "detourTolerance": copy.deepcopy(score.detour_tolerance),
                    "timeWindowFeasible": fits_current_schedule,
                    "scheduleSlackMinutes": slack,
                    "scheduleProjectionRequired": False,
                    "timeWindowPolicy": "current_schedule_verified",
                    "mobilityProfile": {
                        **copy.deepcopy(score.mobility_profile or {}),
                        "evidenceSource": "provider_route_matrix",
                        "modes": [str(leg["mode"])],
                        "walkingDistanceMeters": int(float(leg["walkingDistanceMeters"])),
                        "transferCount": int(float(leg["transferCount"])),
                        "waitSeconds": int(float(leg["waitSeconds"])),
                    },
                    "routeDecisionContract": copy.deepcopy(normalized_contract),
                    "legs": {
                        "previousToCandidate": leg,
                        "previousToNext": baseline_leg,
                    },
                    "baselineRouteOptionId": str(baseline_row["id"] or ""),
                    "candidateRouteOptionId": str(candidate_row["id"] or ""),
                    "routeSelectionChanged": str(baseline_row["id"] or "") != str(candidate_row["id"] or ""),
                    "segmentId": pair[1],
                    "baseVersionId": str(base_version_id or ""),
                    "candidateAmapId": str(right["amapId"]),
                    "fromSegmentId": pair[0],
                    "networkRequired": True,
                    "proofProducer": "public_route_writer_selected_row_rebuild",
                }
            )
        snapshot["routeDecisionContract"] = copy.deepcopy(route_decision_contract)
        snapshot["routeInsertionProofs"] = proofs
        snapshot["routeMatrixExpectedPairs"] = [list(pair) for pair in expected_pairs]
        issues = AgentVerifierService(self.db).validate_route_matrix_snapshot_before_write(
            plan_id,
            snapshot,
        )
        if issues:
            self._raise_public_route_matrix_error(
                "provider_route_matrix_snapshot_invalid",
                validation_errors=issues,
            )

    def _selected_route_rows_snapshot(self, plan_id: str) -> list[dict[str, Any]]:
        rows = self.db.execute(
            """
            SELECT *
            FROM route_options
            WHERE plan_id = ? AND is_selected = 1
            ORDER BY COALESCE(from_segment_id, ''), COALESCE(to_segment_id, ''), id
            """,
            (plan_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def _snapshot_adjacent_route_matrix_pairs(
        self,
        snapshot: dict[str, Any],
    ) -> dict[tuple[str, str], dict[str, Any]]:
        days = snapshot.get("days")
        if not isinstance(days, list):
            self._raise_public_route_matrix_error("provider_route_snapshot_days_invalid")
        expected: dict[tuple[str, str], dict[str, Any]] = {}
        seen_day_ids: set[str] = set()
        seen_anchor_ids: set[str] = set()
        for day in days:
            if not isinstance(day, dict):
                self._raise_public_route_matrix_error("provider_route_snapshot_days_invalid")
            day_id = str(day.get("id") or "")
            segments = day.get("segments")
            if not day_id or day_id in seen_day_ids or not isinstance(segments, list):
                self._raise_public_route_matrix_error("provider_route_snapshot_days_invalid")
            seen_day_ids.add(day_id)
            anchors: list[dict[str, Any]] = []
            for route_order, segment in enumerate(segments, start=1):
                if not isinstance(segment, dict):
                    self._raise_public_route_matrix_error("provider_route_snapshot_segment_invalid")
                if not self._snapshot_segment_is_route_anchor(segment):
                    continue
                segment_id = str(segment.get("id") or "")
                poi = segment.get("poi") if isinstance(segment.get("poi"), dict) else {}
                poi_id = str(poi.get("id") or "")
                point = self._route_matrix_point_from_snapshot_segment(
                    segment,
                    day_id=day_id,
                    route_order=route_order,
                )
                if (
                    not segment_id
                    or segment_id in seen_anchor_ids
                    or not poi_id
                    or not self._canonical_route_matrix_point(point)
                ):
                    self._raise_public_route_matrix_error("provider_route_endpoint_identity_invalid")
                seen_anchor_ids.add(segment_id)
                anchors.append(
                    {
                        "segmentId": segment_id,
                        "poiId": poi_id,
                        "dayId": day_id,
                        "routeOrder": route_order,
                        "point": point,
                    }
                )
            for left, right in zip(anchors, anchors[1:]):
                pair = (str(left["segmentId"]), str(right["segmentId"]))
                if pair in expected:
                    self._raise_public_route_matrix_error("provider_route_snapshot_pair_ambiguous")
                expected[pair] = {
                    "dayId": day_id,
                    "fromPoiId": str(left["poiId"]),
                    "toPoiId": str(right["poiId"]),
                    "fromRouteOrder": int(left["routeOrder"]),
                    "toRouteOrder": int(right["routeOrder"]),
                    "fromPoint": left["point"],
                    "toPoint": right["point"],
                }
        return expected

    def _snapshot_segment_is_route_anchor(self, segment: dict[str, Any]) -> bool:
        kind = str(segment.get("kind") or "")
        metadata = segment.get("semanticMetadata")
        if kind in {"visit", "activity"}:
            return bool(not isinstance(metadata, dict) or metadata.get("routeAnchor", True))
        if kind == "park":
            poi = segment.get("poi") if isinstance(segment.get("poi"), dict) else {}
            return ItineraryService(self.db)._is_grounded_park_route_anchor(
                metadata,
                POI(
                    id=str(poi.get("id") or ""),
                    name=poi.get("name"),
                    city=str(poi.get("city") or ""),
                    category=str(poi.get("category") or ""),
                    source=poi.get("source"),
                    source_note=poi.get("sourceNote"),
                    amap_id=poi.get("amapId") or poi.get("amap_id"),
                    longitude=poi.get("longitude"),
                    latitude=poi.get("latitude"),
                    confidence=poi.get("confidence"),
                ),
                notes=str(segment.get("notes") or ""),
            )
        if kind not in {"meal", "shopping"}:
            return False
        normalized = metadata if isinstance(metadata, dict) else {}
        return bool(normalized.get("routeAnchor")) and str(normalized.get("groundingStatus") or "") not in {
            "not_required",
            "optional_waiting",
            "draft_only",
            "waiting_for_poi_grounding",
            "area_unresolved",
            "provider_rate_limited",
        }

    def _selected_route_row_map(
        self,
        rows: list[dict[str, Any]],
        expected_pairs: dict[tuple[str, str], dict[str, Any]],
        *,
        stage: str,
    ) -> dict[tuple[str, str], dict[str, Any]]:
        by_pair: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            pair = (
                str(row["from_segment_id"] or ""),
                str(row["to_segment_id"] or ""),
            )
            if not all(pair):
                self._raise_public_route_matrix_error("provider_route_endpoints_missing")
            if pair in by_pair:
                code = (
                    "provider_route_baseline_pair_ambiguous"
                    if stage == "baseline"
                    else "provider_route_selected_pair_ambiguous"
                )
                self._raise_public_route_matrix_error(code)
            by_pair[pair] = row
        actual_pairs = set(by_pair)
        required_pairs = set(expected_pairs)
        if actual_pairs != required_pairs:
            missing = sorted(required_pairs - actual_pairs)
            extra = sorted(actual_pairs - required_pairs)
            code = (
                "provider_route_baseline_pairs_mismatch"
                if stage == "baseline"
                else "provider_route_selected_pairs_mismatch"
            )
            self._raise_public_route_matrix_error(
                code,
                validation_errors=[
                    code,
                    *[f"missing:{left}->{right}" for left, right in missing],
                    *[f"extra:{left}->{right}" for left, right in extra],
                ],
            )
        return by_pair

    def _validated_selected_route_pair_leg(
        self,
        plan_id: str,
        row: dict[str, Any],
        *,
        expected: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        pair = (
            str(row["from_segment_id"] or ""),
            str(row["to_segment_id"] or ""),
        )
        left_row = self._segment_with_poi(plan_id, pair[0])
        right_row = self._segment_with_poi(plan_id, pair[1])
        if left_row is None or right_row is None:
            self._raise_public_route_matrix_error("provider_route_endpoints_missing")
        if (
            str(row["from_poi_id"] or "") != str(expected["fromPoiId"])
            or str(row["to_poi_id"] or "") != str(expected["toPoiId"])
            or str(left_row["poi_id"] or "") != str(expected["fromPoiId"])
            or str(right_row["poi_id"] or "") != str(expected["toPoiId"])
            or str(left_row["day_id"] or "") != str(expected["dayId"])
            or str(right_row["day_id"] or "") != str(expected["dayId"])
            or int(left_row["segment_order"] or 0) != int(expected["fromRouteOrder"])
            or int(right_row["segment_order"] or 0) != int(expected["toRouteOrder"])
        ):
            self._raise_public_route_matrix_error("provider_route_endpoints_mismatch")
        left = self._route_matrix_point_from_row(left_row)
        right = self._route_matrix_point_from_row(right_row)
        if not self._canonical_route_matrix_point(left) or not self._canonical_route_matrix_point(right):
            self._raise_public_route_matrix_error("provider_route_endpoint_identity_invalid")
        if not self._route_matrix_point_matches_snapshot(left, expected["fromPoint"]):
            self._raise_public_route_matrix_error("provider_route_endpoints_mismatch")
        if not self._route_matrix_point_matches_snapshot(right, expected["toPoint"]):
            self._raise_public_route_matrix_error("provider_route_endpoints_mismatch")
        return left, right, self._selected_route_row_matrix_leg(row, left=left, right=right)

    @staticmethod
    def _route_matrix_point_matches_snapshot(current: dict[str, Any], expected: dict[str, Any]) -> bool:
        try:
            same_coordinates = math.isclose(
                float(current["longitude"]),
                float(expected["longitude"]),
                rel_tol=0,
                abs_tol=1e-7,
            ) and math.isclose(
                float(current["latitude"]),
                float(expected["latitude"]),
                rel_tol=0,
                abs_tol=1e-7,
            )
        except (KeyError, TypeError, ValueError):
            return False
        return bool(
            same_coordinates
            and str(current.get("segmentId") or "") == str(expected.get("segmentId") or "")
            and str(current.get("dayId") or "") == str(expected.get("dayId") or "")
            and str(current.get("amapId") or "") == str(expected.get("amapId") or "")
            and str(current.get("source") or "") == str(expected.get("source") or "")
            and int(current.get("routeOrder") or 0) == int(expected.get("routeOrder") or 0)
            and str(current.get("startTime") or "") == str(expected.get("startTime") or "")
            and str(current.get("endTime") or "") == str(expected.get("endTime") or "")
        )

    def _selected_route_row_matrix_leg(
        self,
        row: sqlite3.Row,
        *,
        left: dict[str, Any],
        right: dict[str, Any],
    ) -> dict[str, Any]:
        if (
            str(row["provider"] or "") != AMAP_ROUTE_SOURCE
            or str(row["source"] or "") != AMAP_ROUTE_SOURCE
            or row["error_json"]
        ):
            self._raise_public_route_matrix_error("provider_route_source_invalid")
        payload = self._route_row_json_object(row["provider_payload_json"])
        if payload is None or route_evidence_status(provider_payload=payload) != "verified":
            self._raise_public_route_matrix_error("provider_route_status_invalid")
        polyline = self._route_row_json_list(row["polyline_json"])
        steps = self._route_row_json_list(row["steps_json"])
        if not polyline or steps is None:
            self._raise_public_route_matrix_error("provider_route_payload_invalid")
        mode = str(row["mode"] or row["transport_mode"] or "").strip()
        try:
            distance = float(row["distance_meters"])
            duration = float(row["duration_seconds"])
            cost_amount = float(row["cost_amount"] or 0)
            queried_at = datetime.fromisoformat(str(row["queried_at"] or "").replace("Z", "+00:00"))
        except (TypeError, ValueError):
            self._raise_public_route_matrix_error("provider_route_metrics_invalid")
        if queried_at.tzinfo is None or queried_at.utcoffset() is None:
            self._raise_public_route_matrix_error("provider_route_query_time_invalid")
        age_seconds = (datetime.now(timezone.utc) - queried_at.astimezone(timezone.utc)).total_seconds()
        if age_seconds < -60 or age_seconds > ROUTE_CACHE_TTL_SECONDS:
            self._raise_public_route_matrix_error("provider_route_query_stale")
        if (
            not mode
            or not math.isfinite(distance)
            or distance <= 0
            or not math.isfinite(duration)
            or duration <= 0
            or not math.isfinite(cost_amount)
            or cost_amount < 0
        ):
            self._raise_public_route_matrix_error("provider_route_metrics_invalid")
        walking, walking_source = self._route_row_cost_component(
            payload,
            ("walkingDistanceMeters", "walking_distance_meters", "walking_distance"),
            fallback=sum(
                max(0.0, self._route_row_float(step.get("distance")))
                for step in steps
                if isinstance(step, dict) and str(step.get("mode") or "").casefold() == "walking"
            ),
            fallback_source="normalized_route_steps",
        )
        transfer, transfer_source = self._route_row_cost_component(
            payload,
            ("transferCount", "transfer_count", "transfers"),
            fallback=max(
                0,
                len(
                    [
                        step
                        for step in steps
                        if isinstance(step, dict)
                        and str(step.get("mode") or "").casefold() in {"transit", "bus", "subway", "rail"}
                    ]
                )
                - 1,
            ),
            fallback_source="normalized_route_steps",
        )
        wait, wait_source = self._route_row_cost_component(
            payload,
            ("waitSeconds", "wait_seconds"),
            fallback=0.0,
            fallback_source="included_in_provider_duration",
        )
        risk = max(
            self._route_row_float(left.get("riskPenaltyMinutes")),
            self._route_row_float(right.get("riskPenaltyMinutes")),
        )
        return {
            "fromSegmentId": str(left["segmentId"]),
            "toSegmentId": str(right["segmentId"]),
            "fromAmapId": str(left["amapId"]),
            "toAmapId": str(right["amapId"]),
            "provider": AMAP_ROUTE_SOURCE,
            "source": AMAP_ROUTE_SOURCE,
            "mode": mode,
            "distanceMeters": int(distance),
            "durationSeconds": int(duration),
            "queriedAt": queried_at.isoformat(),
            "walkingDistanceMeters": walking,
            "transferCount": transfer,
            "waitSeconds": wait,
            "riskPenaltyMinutes": risk,
            "polyline": copy.deepcopy(polyline),
            "steps": copy.deepcopy(steps),
            "providerPayload": copy.deepcopy(payload),
            "costAmount": cost_amount,
            "costCurrency": str(row["cost_currency"] or "CNY"),
            "costComponentProvenance": {
                "walkingDistance": walking_source,
                "transferCount": transfer_source,
                "wait": wait_source,
                "risk": "candidate_or_anchor_contract" if risk > 0 else "no_explicit_risk_penalty",
            },
        }

    @staticmethod
    def _canonical_route_matrix_point(point: Optional[dict[str, Any]]) -> bool:
        if not isinstance(point, dict):
            return False
        amap_id = str(point.get("amapId") or "").strip()
        try:
            longitude = float(point.get("longitude"))
            latitude = float(point.get("latitude"))
        except (TypeError, ValueError):
            return False
        return bool(
            re.fullmatch(r"B[0-9A-Z]{8,31}", amap_id)
            and math.isfinite(longitude)
            and math.isfinite(latitude)
            and -180 <= longitude <= 180
            and -90 <= latitude <= 90
            and (longitude != 0 or latitude != 0)
        )

    @staticmethod
    def _route_row_json_object(value: Any) -> Optional[dict[str, Any]]:
        try:
            parsed = json.loads(value or "{}")
        except (TypeError, json.JSONDecodeError):
            return None
        return parsed if isinstance(parsed, dict) else None

    @staticmethod
    def _route_row_json_list(value: Any) -> Optional[list[Any]]:
        try:
            parsed = json.loads(value or "[]")
        except (TypeError, json.JSONDecodeError):
            return None
        return parsed if isinstance(parsed, list) else None

    @staticmethod
    def _route_row_float(value: Any) -> float:
        try:
            number = float(value or 0)
        except (TypeError, ValueError):
            return 0.0
        return number if math.isfinite(number) else 0.0

    @classmethod
    def _route_row_cost_component(
        cls,
        payload: dict[str, Any],
        keys: tuple[str, ...],
        *,
        fallback: float,
        fallback_source: str,
    ) -> tuple[float, str]:
        for key in keys:
            if key not in payload or payload.get(key) is None:
                continue
            try:
                value = float(payload[key])
            except (TypeError, ValueError):
                cls._raise_public_route_matrix_error("provider_route_cost_component_invalid")
            if not math.isfinite(value) or value < 0:
                cls._raise_public_route_matrix_error("provider_route_cost_component_invalid")
            return value, "provider_payload"
        if not math.isfinite(float(fallback)) or float(fallback) < 0:
            cls._raise_public_route_matrix_error("provider_route_cost_component_invalid")
        return float(fallback), fallback_source

    @staticmethod
    def _raise_public_route_matrix_error(
        code: str,
        *,
        validation_errors: Optional[list[str]] = None,
    ) -> None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": code,
                "validationErrors": list(validation_errors or [code]),
            },
        )

    def _materialize_agent_snapshot_route_anchors(
        self,
        plan_id: str,
        operations: list[ItineraryPatchOperation],
        *,
        source_type: str,
    ) -> list[ItineraryPatchOperation]:
        """Resolve initial route anchors read-only before the writer starts.

        Only an exact, verifier-grade AMap identity is materialized.  A fuzzy
        area anchor or provider failure remains unresolved and is rejected by
        the subsequent route-matrix gate without changing itinerary tables.
        """
        if source_type != "agent":
            return operations
        plan_row = self.db.execute("SELECT city FROM itinerary_plans WHERE id = ?", (plan_id,)).fetchone()
        plan_city = str(plan_row["city"] or "") if plan_row else ""
        materialized: list[ItineraryPatchOperation] = []
        for operation in operations:
            if operation.op != "replace_itinerary" or not isinstance(operation.full_itinerary, dict):
                materialized.append(operation)
                continue
            snapshot = copy.deepcopy(operation.full_itinerary)
            snapshot_city = str(snapshot.get("city") or plan_city)
            for day in snapshot.get("days") or []:
                if not isinstance(day, dict):
                    continue
                route_segments = [
                    segment
                    for segment in day.get("segments") or []
                    if isinstance(segment, dict) and str(segment.get("kind") or "activity") in {"visit", "activity"}
                ]
                if len(route_segments) < 2:
                    continue
                for segment in route_segments:
                    poi = segment.get("poi") if isinstance(segment.get("poi"), dict) else {}
                    if poi.get("source") != "agent-text-timeline":
                        continue
                    query = str(poi.get("name") or "").strip()
                    if not query:
                        continue
                    try:
                        response = MapPoiService().search(
                            city=str(poi.get("city") or snapshot_city),
                            keyword=query,
                            category="all",
                            limit=8,
                        )
                    except Exception:
                        continue
                    exact = [
                        candidate for candidate in response.pois if self._exact_route_anchor_candidate(query, candidate)
                    ]
                    if len(exact) != 1:
                        continue
                    candidate = exact[0]
                    original_id = str(poi.get("id") or f"poi_{uuid4().hex[:12]}")
                    candidate_payload = candidate.model_dump(
                        by_alias=True,
                        mode="json",
                    )
                    segment["poi"] = {
                        **candidate_payload,
                        "id": original_id,
                        "amapId": candidate.id,
                        "source": AMAP_PLACE_SOURCE,
                        "confidence": max(float(candidate.confidence), 0.8),
                    }
            materialized.append(operation.model_copy(update={"full_itinerary": snapshot}))
        return materialized

    @staticmethod
    def _exact_route_anchor_candidate(query: str, candidate: MapPoiResponse) -> bool:
        normalized_query = re.sub(r"[\s\-_,.()（）·・，。/]+", "", str(query or "")).casefold()
        normalized_name = re.sub(r"[\s\-_,.()（）·・，。/]+", "", str(candidate.name or "")).casefold()
        try:
            longitude = float(candidate.longitude)
            latitude = float(candidate.latitude)
        except (TypeError, ValueError):
            return False
        return bool(
            normalized_query
            and normalized_query == normalized_name
            and candidate.source == AMAP_PLACE_SOURCE
            and candidate.id
            and float(candidate.confidence or 0) >= 0.8
            and math.isfinite(longitude)
            and math.isfinite(latitude)
            and -180 <= longitude <= 180
            and -90 <= latitude <= 90
        )

    def _ensure_agent_route_matrix_proofs(
        self,
        plan_id: str,
        operations: list[ItineraryPatchOperation],
        planning_context: dict,
        *,
        base_version_id: Optional[str],
        preference_summary: Optional[str],
    ) -> list[str]:
        """Generate every decisive Provider proof before the first business write."""
        del preference_summary  # Reserved for future mobility-profile derivation.
        route_operations = [
            operation
            for operation in operations
            if operation.op == "replace_itinerary"
            or (
                operation.op
                in {
                    "add_segment",
                    "replace_segment_poi",
                    "replace_segment_poi_from_candidate",
                }
                and operation.amap_poi is not None
            )
        ]
        requires_final_coverage = self._requires_provider_matrix_preflight(operations)
        if not route_operations and not requires_final_coverage:
            return []
        route_decision_contract = self.route_insertion_scorer.normalized_route_decision_contract(
            planning_context.get("routeDecisionContract")
        )
        if route_decision_contract is None:
            return ["provider_route_matrix_preflight_failed:route_decision_contract_missing_or_invalid"]
        # The normalized value is local verifier input only.  Preserve the
        # exact server-owned envelope in ``planning_context`` for snapshot
        # persistence; downstream scorers normalize it at their boundary.
        supplied = planning_context.get("routeInsertionProofs")
        if supplied is not None and not isinstance(supplied, list):
            return ["routeInsertionProofs must be a list"]
        supplied_proofs = [copy.deepcopy(item) for item in (supplied or []) if isinstance(item, dict)]
        if len(supplied_proofs) != len(supplied or []):
            return ["routeInsertionProofs must contain only objects"]
        if any(operation.op == "replace_itinerary" for operation in route_operations):
            if len(operations) != 1:
                return ["Agent replace_itinerary route preflight requires an exclusive operation"]
            if supplied_proofs:
                # A complete upstream initial-snapshot matrix can be reused;
                # the validator below still binds every leg to this base and
                # every exact final endpoint.
                proofs, errors = supplied_proofs, []
            else:
                proofs, errors = self._initial_snapshot_route_matrix_proofs(
                    plan_id,
                    route_operations[0],
                    planning_context,
                    base_version_id=base_version_id,
                )
            if not errors:
                planning_context["routeMatrixExpectedPairs"] = [
                    [
                        str(proof.get("fromSegmentId") or ""),
                        str(proof.get("segmentId") or ""),
                    ]
                    for proof in proofs
                    if str(proof.get("fromSegmentId") or "") and str(proof.get("segmentId") or "")
                ]
        else:
            unsupported_compound = {
                "move_segment",
                "reorder_segments",
            }
            if route_operations and any(operation.op in unsupported_compound for operation in operations):
                return ["Agent route decision cannot share a patch with an unsupported topology mutation"]
            decision_proofs = [
                proof for proof in supplied_proofs if str(proof.get("proofType") or "") != "adjacent_route_coverage"
            ]
            if route_operations and not decision_proofs:
                decision_proofs, errors = self._local_route_matrix_proofs(
                    plan_id,
                    route_operations,
                    operations,
                    planning_context,
                    base_version_id=base_version_id,
                )
            else:
                errors = []
            if not errors and requires_final_coverage:
                coverage_proofs, expected_pairs, coverage_errors = self._final_route_coverage_proofs(
                    plan_id,
                    operations,
                    planning_context,
                    reusable_proofs=decision_proofs,
                    base_version_id=base_version_id,
                )
                errors.extend(coverage_errors)
                planning_context["routeMatrixExpectedPairs"] = [list(pair) for pair in sorted(expected_pairs)]
                proofs = [*decision_proofs, *coverage_proofs]
            else:
                proofs = decision_proofs
        if errors:
            return errors
        planning_context["routeInsertionProofs"] = proofs
        return []

    @staticmethod
    def _requires_provider_matrix_preflight(
        operations: list[ItineraryPatchOperation],
    ) -> bool:
        for operation in operations:
            if operation.op == "add_segment":
                if operation.amap_poi is not None:
                    return True
                continue
            if operation.op in {
                "replace_itinerary",
                "replace_segment_poi",
                "replace_segment_poi_from_candidate",
                "remove_segment",
                "move_segment",
                "reorder_segments",
                "replace_segment_start_time",
                "replace_segment_duration",
                "replace_transport_mode",
                "refresh_routes_for_day",
            }:
                return True
        return False

    @staticmethod
    def _invalidates_simple_open_route_assignment(
        operations: list[ItineraryPatchOperation],
    ) -> bool:
        """Return whether an edit changes identities bound into route evidence.

        Start-time and duration edits still require the normal route refresh,
        but they do not change the frozen ordered AMap pairs or transport mode.
        POI, order, membership, or mode changes must discard the old evidence.
        """

        for operation in operations:
            if operation.op == "add_segment" and operation.amap_poi is not None:
                return True
            if operation.op in {
                "replace_itinerary",
                "replace_segment_poi",
                "replace_segment_poi_from_candidate",
                "remove_segment",
                "move_segment",
                "reorder_segments",
                "replace_transport_mode",
                "refresh_routes_for_day",
            }:
                return True
        return False

    def _final_route_coverage_proofs(
        self,
        plan_id: str,
        operations: list[ItineraryPatchOperation],
        planning_context: dict,
        *,
        reusable_proofs: list[dict],
        base_version_id: Optional[str],
    ) -> tuple[list[dict], set[tuple[str, str]], list[str]]:
        route_decision_contract = self.route_insertion_scorer.normalized_route_decision_contract(
            planning_context.get("routeDecisionContract")
        )
        if route_decision_contract is None:
            return [], set(), ["provider_route_matrix_preflight_failed:route_decision_contract_missing_or_invalid"]
        points_by_day, errors = self._simulate_final_route_matrix_points(
            plan_id,
            operations,
            planning_context,
        )
        if errors:
            return [], set(), errors
        reusable_legs: dict[tuple[str, str], dict[str, Any]] = {}
        for proof in reusable_proofs:
            if str(proof.get("status") or "") != "passed":
                continue
            legs = proof.get("legs") if isinstance(proof.get("legs"), dict) else {}
            for key in ("previousToCandidate", "candidateToNext"):
                leg = legs.get(key)
                if not isinstance(leg, dict):
                    continue
                pair = (
                    str(leg.get("fromSegmentId") or ""),
                    str(leg.get("toSegmentId") or ""),
                )
                if all(pair) and self._provider_matrix_leg_complete_before_write(leg):
                    reusable_legs[pair] = leg

        transport_mode = self._route_preflight_transport_mode(
            plan_id,
            operations,
            planning_context,
        )
        if not transport_mode:
            return (
                [],
                set(),
                ["provider_route_matrix_preflight_failed:route_transport_mode_missing_or_conflict"],
            )
        expected_pairs: set[tuple[str, str]] = set()
        proofs: list[dict] = []
        for day_id, points in sorted(points_by_day.items()):
            for pair_index, (left, right) in enumerate(
                zip(points, points[1:]),
                start=1,
            ):
                pair = (
                    str(left.get("segmentId") or ""),
                    str(right.get("segmentId") or ""),
                )
                expected_pairs.add(pair)
                leg = reusable_legs.get(pair)
                if leg is None or (
                    str(leg.get("fromAmapId") or "") != str(left.get("amapId") or "")
                    or str(leg.get("toAmapId") or "") != str(right.get("amapId") or "")
                ):
                    leg = self.provider_route_insertion_service.verified_leg(
                        plan_id=(f"{plan_id}_final_route_preflight_{day_id}_{pair_index}"),
                        left=left,
                        right=right,
                        transport_mode=transport_mode,
                    )
                if leg is None:
                    return proofs, expected_pairs, ["provider_route_matrix_preflight_failed:provider_route_leg_missing"]
                fits_current_gap, slack = self._adjacent_leg_time_window(
                    left,
                    right,
                    leg,
                )
                time_window_locked = bool(left.get("timeWindowLocked") or right.get("timeWindowLocked"))
                if time_window_locked and not fits_current_gap:
                    return (
                        proofs,
                        expected_pairs,
                        ["provider_route_matrix_preflight_failed:route_time_window_infeasible"],
                    )
                proofs.append(
                    {
                        "proofType": "adjacent_route_coverage",
                        "operation": "final_route_pair_coverage",
                        "status": "passed",
                        "failureReason": None,
                        "networkVerified": True,
                        "detourLevel": "not_applicable",
                        "generalizedCostDelta": None,
                        "detourRatio": None,
                        "detourTolerance": None,
                        "timeWindowFeasible": True,
                        "scheduleSlackMinutes": slack,
                        "scheduleProjectionRequired": not fits_current_gap,
                        "timeWindowPolicy": (
                            "locked_fail_closed" if time_window_locked else "flexible_schedule_projection"
                        ),
                        "mobilityProfile": {
                            **copy.deepcopy(route_decision_contract["mobilityProfile"]),
                            "modes": [str(leg.get("mode") or transport_mode)],
                        },
                        "routeDecisionContract": copy.deepcopy(route_decision_contract),
                        "legs": {"previousToCandidate": leg},
                        "segmentId": pair[1],
                        "baseVersionId": str(base_version_id or ""),
                        "candidateAmapId": str(right.get("amapId") or ""),
                        "fromSegmentId": pair[0],
                        "networkRequired": True,
                        "proofProducer": "itinerary_patch_writer_preflight",
                    }
                )
        return proofs, expected_pairs, []

    def _simulate_final_route_matrix_points(
        self,
        plan_id: str,
        operations: list[ItineraryPatchOperation],
        planning_context: dict,
    ) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
        touched_day_ids: set[str] = set()
        target_rows: dict[str, sqlite3.Row] = {}
        for operation in operations:
            if operation.day_id:
                touched_day_ids.add(str(operation.day_id))
            if operation.target_day_id:
                touched_day_ids.add(str(operation.target_day_id))
            if operation.segment_id:
                row = self._segment_with_poi(
                    plan_id,
                    str(operation.segment_id),
                )
                if row is not None:
                    target_rows[str(operation.segment_id)] = row
                    touched_day_ids.add(str(row["day_id"] or ""))

        points_by_day = {
            day_id: [
                point
                for row in self._route_anchor_rows_for_day(plan_id, day_id)
                if (point := self._route_matrix_point_from_row(row)) is not None
            ]
            for day_id in touched_day_ids
            if day_id
        }
        duration_overrides = {
            str(operation.segment_id or ""): int(operation.duration_minutes or 0)
            for operation in operations
            if operation.op == "replace_segment_duration" and operation.segment_id
        }

        for operation in operations:
            segment_id = str(operation.segment_id or "")
            if operation.op == "add_segment":
                day_id = str(operation.day_id or "")
                points = points_by_day.setdefault(day_id, [])
                next_order = max([int(point.get("routeOrder") or 0) for point in points] or [0]) + 1
                start_time = operation.start_time or operation.value or self._next_segment_start_time(plan_id, day_id)
                candidate = self._route_matrix_point_from_operation(
                    operation,
                    day_id=day_id,
                    start_time=start_time,
                    duration_minutes=int(operation.duration_minutes or 30),
                    route_order=next_order,
                )
                if candidate is None:
                    return {}, ["provider_route_matrix_preflight_candidate_invalid"]
                candidate["timeWindowLocked"] = bool(operation.start_time)
                candidate["scheduleMutated"] = True
                points.append(candidate)
                continue
            if operation.op in {
                "replace_segment_poi",
                "replace_segment_poi_from_candidate",
            }:
                current = target_rows.get(segment_id)
                if current is None:
                    return {}, ["provider_route_matrix_preflight_target_missing"]
                duration = duration_overrides.get(segment_id)
                if not duration:
                    duration = self._replacement_route_duration(
                        current,
                        operation,
                        planning_context,
                    )
                candidate = self._route_matrix_point_from_operation(
                    operation,
                    day_id=str(current["day_id"] or ""),
                    start_time=str(current["start_time"] or ""),
                    duration_minutes=duration,
                    route_order=int(current["segment_order"] or 0),
                )
                if candidate is None:
                    return {}, ["provider_route_matrix_preflight_candidate_invalid"]
                candidate["timeWindowLocked"] = bool(
                    self._row_time_window_locked(current) or segment_id in duration_overrides
                )
                candidate["kind"] = str(current["kind"] or "visit")
                candidate["semanticMetadata"] = {
                    **self._row_semantic_metadata(current),
                    **copy.deepcopy(operation.semantic_metadata or {}),
                }
                candidate["scheduleMutated"] = True
                points = points_by_day.setdefault(
                    str(current["day_id"] or ""),
                    [],
                )
                points[:] = [point for point in points if str(point.get("segmentId") or "") != segment_id]
                points.append(candidate)
                continue
            if operation.op == "remove_segment":
                for points in points_by_day.values():
                    points[:] = [point for point in points if str(point.get("segmentId") or "") != segment_id]
                continue
            if operation.op == "move_segment":
                current = target_rows.get(segment_id)
                if current is None:
                    return {}, ["provider_route_matrix_preflight_target_missing"]
                source_day_id = str(current["day_id"] or "")
                target_day_id = str(operation.target_day_id or source_day_id)
                source_points = points_by_day.setdefault(source_day_id, [])
                current_point = next(
                    (point for point in source_points if str(point.get("segmentId") or "") == segment_id),
                    None,
                )
                if current_point is None:
                    return {}, ["provider_route_matrix_preflight_target_missing"]
                source_points[:] = [point for point in source_points if str(point.get("segmentId") or "") != segment_id]
                moved = copy.deepcopy(current_point)
                moved["dayId"] = target_day_id
                moved["routeOrder"] = (
                    max(
                        [int(point.get("routeOrder") or 0) for point in points_by_day.setdefault(target_day_id, [])]
                        or [0]
                    )
                    + 1
                )
                if operation.start_time:
                    duration = self._point_duration(moved)
                    moved["startTime"] = str(operation.start_time)
                    moved["endTime"] = self._format_minutes(self._minutes(str(operation.start_time)) + duration)
                    moved["timeWindowLocked"] = True
                    moved["scheduleMutated"] = True
                points_by_day[target_day_id].append(moved)
                continue
            if operation.op == "reorder_segments":
                order = [str(item) for item in (operation.ordered_segment_ids or [])]
                if not order:
                    return {}, ["provider_route_matrix_preflight_reorder_missing_order"]
                positions = {segment_id: index for index, segment_id in enumerate(order, start=1)}
                for points in points_by_day.values():
                    for point in points:
                        segment_id = str(point.get("segmentId") or "")
                        if segment_id in positions:
                            point["routeOrder"] = positions[segment_id]
                continue
            if operation.op in {
                "replace_segment_start_time",
                "replace_segment_duration",
            }:
                point = next(
                    (
                        item
                        for points in points_by_day.values()
                        for item in points
                        if str(item.get("segmentId") or "") == segment_id
                    ),
                    None,
                )
                if point is None:
                    continue
                if operation.op == "replace_segment_start_time":
                    current = target_rows.get(segment_id)
                    duration = self._point_duration(point)
                    start_time = str(operation.start_time or operation.value or "")
                    point["startTime"] = start_time
                    point["endTime"] = self._format_minutes(self._minutes(start_time) + duration)
                    if current is not None and planning_context.get("_serverUserTimelineMutation") is True:
                        point["semanticMetadata"] = self._semantic_metadata_for_user_start_time(
                            current,
                            start_time=start_time,
                            duration_minutes=duration,
                        )
                else:
                    point["endTime"] = self._format_minutes(
                        self._minutes(str(point.get("startTime") or "")) + int(operation.duration_minutes or 0)
                    )
                point["timeWindowLocked"] = True
                point["scheduleMutated"] = True

        for points in points_by_day.values():
            points.sort(
                key=lambda point: (
                    int(point.get("routeOrder") or 0),
                    str(point.get("startTime") or ""),
                    str(point.get("segmentId") or ""),
                )
            )
            for point in points:
                if point.get("scheduleMutated") is not True:
                    continue
                temporal_failures = ItineraryScheduleService.temporal_failures(
                    {
                        "id": point.get("segmentId"),
                        "kind": point.get("kind"),
                        "startTime": point.get("startTime"),
                        "endTime": point.get("endTime"),
                        "semanticMetadata": point.get("semanticMetadata") or {},
                    }
                )
                if temporal_failures:
                    return {}, [
                        "provider_route_matrix_preflight_failed:"
                        + str(temporal_failures[0].get("code") or "route_time_window_infeasible")
                    ]
        return points_by_day, []

    def _local_route_matrix_proofs(
        self,
        plan_id: str,
        route_operations: list[ItineraryPatchOperation],
        all_operations: list[ItineraryPatchOperation],
        planning_context: dict,
        *,
        base_version_id: Optional[str],
    ) -> tuple[list[dict], list[str]]:
        current_targets: dict[str, sqlite3.Row] = {}
        touched_day_ids: set[str] = set()
        for operation in route_operations:
            if operation.op == "add_segment":
                if operation.day_id:
                    touched_day_ids.add(operation.day_id)
                continue
            row = self._segment_with_poi(plan_id, operation.segment_id or "")
            if row is None:
                return [], ["provider_route_matrix_preflight_target_missing"]
            current_targets[str(operation.segment_id)] = row
            touched_day_ids.add(str(row["day_id"]))

        points_by_day: dict[str, list[dict[str, Any]]] = {}
        for day_id in touched_day_ids:
            points_by_day[day_id] = [
                point
                for row in self._route_anchor_rows_for_day(plan_id, day_id)
                if (point := self._route_matrix_point_from_row(row)) is not None
            ]

        next_orders = {
            day_id: max([int(point.get("routeOrder") or 0) for point in points] or [0])
            for day_id, points in points_by_day.items()
        }
        candidate_points: dict[str, dict[str, Any]] = {}
        baseline_points: dict[str, Optional[dict[str, Any]]] = {}
        for operation in route_operations:
            segment_id = str(operation.segment_id or "")
            if operation.op == "add_segment":
                day_id = str(operation.day_id or "")
                next_orders[day_id] = next_orders.get(day_id, 0) + 1
                candidate = self._route_matrix_point_from_operation(
                    operation,
                    day_id=day_id,
                    start_time=(
                        operation.start_time or operation.value or self._next_segment_start_time(plan_id, day_id)
                    ),
                    duration_minutes=int(operation.duration_minutes or 30),
                    route_order=next_orders[day_id],
                )
                baseline_points[segment_id] = None
            else:
                current = current_targets[segment_id]
                explicit_duration = next(
                    (
                        int(item.duration_minutes or 0)
                        for item in all_operations
                        if item.op == "replace_segment_duration" and str(item.segment_id or "") == segment_id
                    ),
                    0,
                )
                duration = explicit_duration or self._replacement_route_duration(
                    current,
                    operation,
                    planning_context,
                )
                candidate = self._route_matrix_point_from_operation(
                    operation,
                    day_id=str(current["day_id"]),
                    start_time=str(current["start_time"] or ""),
                    duration_minutes=duration,
                    route_order=int(current["segment_order"] or 0),
                )
                baseline_points[segment_id] = (
                    self._route_matrix_point_from_row(current) if self._is_route_anchor_row(current) else None
                )
            if candidate is None:
                return [], ["provider_route_matrix_preflight_candidate_invalid"]
            candidate_points[segment_id] = candidate
            day_points = points_by_day.setdefault(str(candidate["dayId"]), [])
            day_points[:] = [point for point in day_points if str(point.get("segmentId") or "") != segment_id]
            day_points.append(candidate)

        for points in points_by_day.values():
            points.sort(
                key=lambda point: (
                    int(point.get("routeOrder") or 0),
                    str(point.get("startTime") or ""),
                    str(point.get("segmentId") or ""),
                )
            )

        transport_mode = self._route_preflight_transport_mode(
            plan_id,
            all_operations,
            planning_context,
        )
        if not transport_mode:
            return [], ["provider_route_matrix_preflight_failed:route_transport_mode_missing_or_conflict"]
        proofs: list[dict] = []
        for operation in route_operations:
            segment_id = str(operation.segment_id or "")
            candidate = candidate_points[segment_id]
            day_points = points_by_day[str(candidate["dayId"])]
            index = next(
                index for index, point in enumerate(day_points) if str(point.get("segmentId") or "") == segment_id
            )
            previous = day_points[index - 1] if index > 0 else None
            following = day_points[index + 1] if index + 1 < len(day_points) else None
            tolerance, tolerance_source = self._route_preflight_tolerance(
                operation,
                current_targets.get(segment_id),
                planning_context,
            )
            result = self.provider_route_insertion_service.evaluate(
                plan_id=f"{plan_id}_writer_preflight_{segment_id}",
                previous=previous,
                candidate=candidate,
                following=following,
                baseline_candidate=baseline_points.get(segment_id),
                transport_mode=transport_mode,
                detour_tolerance=tolerance,
                candidate_duration_minutes=self._point_duration(candidate),
                mobility_profile=self._route_preflight_mobility_profile(operation, planning_context),
                route_decision_contract=planning_context["routeDecisionContract"],
            )
            proof = {
                **result.to_dict(),
                "proofType": "route_insertion_or_replacement",
                "operation": operation.op,
                "segmentId": segment_id,
                "baseVersionId": str(base_version_id or ""),
                "candidateAmapId": str(candidate.get("amapId") or ""),
                "toleranceSource": tolerance_source,
                "networkRequired": previous is not None or following is not None,
                "proofProducer": "itinerary_patch_writer_preflight",
                "routeDecisionContract": copy.deepcopy(planning_context["routeDecisionContract"]),
            }
            proofs.append(proof)
            if not result.passed:
                return proofs, ["provider_route_matrix_preflight_failed:" + str(result.failure_reason or result.status)]
        return proofs, []

    def _initial_snapshot_route_matrix_proofs(
        self,
        plan_id: str,
        operation: ItineraryPatchOperation,
        planning_context: dict,
        *,
        base_version_id: Optional[str],
    ) -> tuple[list[dict], list[str]]:
        snapshot = operation.full_itinerary or {}
        proofs: list[dict] = []
        for day in snapshot.get("days") or []:
            if not isinstance(day, dict):
                continue
            day_id = str(day.get("id") or "")
            potential: list[tuple[dict, Optional[dict[str, Any]]]] = []
            for order, segment in enumerate(day.get("segments") or [], start=1):
                if not isinstance(segment, dict):
                    continue
                kind = str(segment.get("kind") or "activity")
                poi = segment.get("poi") if isinstance(segment.get("poi"), dict) else {}
                semantic = segment.get("semanticMetadata") if isinstance(segment.get("semanticMetadata"), dict) else {}
                route_intent = kind in {"visit", "activity"} or (
                    kind == "meal" and (poi.get("source") == AMAP_PLACE_SOURCE or semantic.get("routeAnchor") is True)
                )
                if not route_intent:
                    continue
                point = self._route_matrix_point_from_snapshot_segment(
                    segment,
                    day_id=day_id,
                    route_order=order,
                )
                potential.append((segment, point))
            if len(potential) < 2:
                continue
            if any(point is None for _segment, point in potential):
                return proofs, ["initial_route_matrix_requires_grounded_amap_anchors"]
            points = [point for _segment, point in potential if point is not None]
            contract = self.route_insertion_scorer.normalized_route_decision_contract(
                planning_context.get("routeDecisionContract")
            )
            provenance = contract.get("provenance") if isinstance(contract, dict) else {}
            signed_mode = (
                normalize_route_mode(str(provenance.get("transportMode") or "").strip())
                if isinstance(provenance, dict)
                else ""
            )
            snapshot_modes = {
                normalize_route_mode(str(segment.get("transportMode") or "").strip())
                for segment, _point in potential
                if str(segment.get("transportMode") or "").strip()
            }
            if len(snapshot_modes) > 1:
                return proofs, ["initial_route_matrix_transport_mode_missing_or_conflict"]
            persisted_mode = next(iter(snapshot_modes), "")
            if signed_mode and persisted_mode and signed_mode != persisted_mode:
                return proofs, ["initial_route_matrix_transport_mode_missing_or_conflict"]
            transport_mode = signed_mode or persisted_mode
            if not transport_mode:
                return proofs, ["initial_route_matrix_transport_mode_missing_or_conflict"]
            for pair_index, (
                (left_segment, left),
                (right_segment, right),
            ) in enumerate(zip(potential, potential[1:]), start=1):
                if left is None or right is None:
                    return proofs, ["initial_route_matrix_requires_grounded_amap_anchors"]
                leg = self.provider_route_insertion_service.verified_leg(
                    plan_id=(f"{plan_id}_initial_route_preflight_{day_id}_{pair_index}"),
                    left=left,
                    right=right,
                    transport_mode=transport_mode,
                )
                if leg is None:
                    return proofs, ["provider_route_matrix_preflight_failed:provider_route_leg_missing"]
                fits_current_gap, slack = self._adjacent_leg_time_window(left, right, leg)
                time_window_locked = self._snapshot_pair_time_window_locked(
                    left_segment,
                    right_segment,
                )
                if time_window_locked and not fits_current_gap:
                    return proofs, ["provider_route_matrix_preflight_failed:route_time_window_infeasible"]
                proofs.append(
                    {
                        "proofType": "adjacent_route_coverage",
                        "operation": "replace_itinerary_route_pair",
                        "status": "passed",
                        "failureReason": None,
                        "networkVerified": True,
                        "detourLevel": "not_applicable",
                        "generalizedCostDelta": None,
                        "detourRatio": None,
                        "detourTolerance": None,
                        "timeWindowFeasible": True,
                        "scheduleSlackMinutes": slack,
                        "scheduleProjectionRequired": not fits_current_gap,
                        "timeWindowPolicy": (
                            "locked_fail_closed" if time_window_locked else "flexible_schedule_projection"
                        ),
                        "mobilityProfile": {
                            **copy.deepcopy(planning_context["routeDecisionContract"]["mobilityProfile"]),
                            "modes": [str(leg.get("mode") or transport_mode)],
                        },
                        "routeDecisionContract": copy.deepcopy(planning_context["routeDecisionContract"]),
                        "legs": {"previousToCandidate": leg},
                        "segmentId": str(right.get("segmentId") or ""),
                        "baseVersionId": str(base_version_id or ""),
                        "candidateAmapId": str(right.get("amapId") or ""),
                        "fromSegmentId": str(left.get("segmentId") or ""),
                        "networkRequired": True,
                        "proofProducer": "itinerary_patch_writer_preflight",
                    }
                )
        return proofs, []

    @staticmethod
    def _snapshot_pair_time_window_locked(left: dict[str, Any], right: dict[str, Any]) -> bool:
        for segment in (left, right):
            semantic = segment.get("semanticMetadata") if isinstance(segment.get("semanticMetadata"), dict) else {}
            estimate = segment.get("estimateMetadata") if isinstance(segment.get("estimateMetadata"), dict) else {}
            duration = estimate.get("duration") if isinstance(estimate.get("duration"), dict) else {}
            if (
                semantic.get("userLocked") is True
                or semantic.get("timeWindowLocked") is True
                or duration.get("userLocked") is True
            ):
                return True
        return False

    def _row_time_window_locked(self, row: sqlite3.Row) -> bool:
        semantic = self._row_semantic_metadata(row)
        estimate = self._estimate_metadata(row)
        duration = estimate.get("duration") if isinstance(estimate.get("duration"), dict) else {}
        return bool(
            semantic.get("userLocked") is True
            or semantic.get("timeWindowLocked") is True
            or duration.get("userLocked") is True
        )

    @staticmethod
    def _row_semantic_metadata(row: sqlite3.Row) -> dict[str, Any]:
        try:
            semantic = json.loads(row["semantic_metadata_json"] or "{}")
            return semantic if isinstance(semantic, dict) else {}
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return {}

    def _semantic_metadata_for_user_start_time(
        self,
        row: sqlite3.Row,
        *,
        start_time: str,
        duration_minutes: int,
    ) -> dict[str, Any]:
        semantic_metadata = self._row_semantic_metadata(row)
        schedule_constraints = (
            semantic_metadata.get("scheduleConstraints")
            if isinstance(semantic_metadata.get("scheduleConstraints"), dict)
            else {}
        )
        schedule_source = str(schedule_constraints.get("source") or "").strip().lower()
        # A previously accepted user clock is authoritative for automatic
        # rescheduling, but a later trusted user timeline mutation must still
        # be able to replace it. Other hard evidence remains immutable here.
        if schedule_constraints.get("hard") is True and schedule_source != "user_explicit_clock":
            return semantic_metadata
        if schedule_source in {
            "locked_activity",
            "verified_opening_evidence",
            "verified_route_arrival",
        }:
            return semantic_metadata
        intent_type = str(semantic_metadata.get("intentType") or row["kind"] or "").strip().lower()
        if intent_type in {"night", "night_view"}:
            return semantic_metadata
        end_time = self._format_minutes(self._minutes(start_time) + max(duration_minutes, 1))
        user_clock = ItineraryScheduleService.schedule_constraint_payload(
            time_window=f"{start_time}-{end_time}",
            start_time=start_time,
            duration=duration_minutes,
            intent_type=intent_type,
            source="user_explicit_clock",
        )
        user_clock.update({"explicitStartTime": start_time, "userLocked": True})
        semantic_metadata["scheduleConstraints"] = {**schedule_constraints, **user_clock}
        semantic_metadata["userLocked"] = True
        semantic_metadata["timeWindowLocked"] = True
        return semantic_metadata

    def _route_matrix_point_from_row(self, row: sqlite3.Row) -> Optional[dict[str, Any]]:
        try:
            longitude = float(row["longitude"])
            latitude = float(row["latitude"])
        except (KeyError, TypeError, ValueError):
            return None
        if (
            str(row["source"] or "") != AMAP_PLACE_SOURCE
            or not str(row["amap_id"] or "")
            or not math.isfinite(longitude)
            or not math.isfinite(latitude)
        ):
            return None
        return {
            "segmentId": str(row["id"] or ""),
            "dayId": str(row["day_id"] or ""),
            "amapId": str(row["amap_id"] or ""),
            "name": str(row["poi_name"] or row["amap_id"] or ""),
            "city": str(row["poi_city"] or ""),
            "category": str(row["poi_category"] or ""),
            "type": str(row["poi_category"] or "route_anchor"),
            "source": AMAP_PLACE_SOURCE,
            "longitude": longitude,
            "latitude": latitude,
            "startTime": str(row["start_time"] or ""),
            "endTime": str(row["end_time"] or ""),
            "routeOrder": int(row["segment_order"] or 0),
            "riskPenaltyMinutes": self._route_risk_penalty(row),
            "timeWindowLocked": self._row_time_window_locked(row),
            "kind": str(row["kind"] or "visit"),
            "semanticMetadata": self._row_semantic_metadata(row),
        }

    def _route_matrix_point_from_operation(
        self,
        operation: ItineraryPatchOperation,
        *,
        day_id: str,
        start_time: str,
        duration_minutes: int,
        route_order: int,
    ) -> Optional[dict[str, Any]]:
        poi = operation.amap_poi
        if poi is None or poi.source != AMAP_PLACE_SOURCE:
            return None
        return {
            "segmentId": str(operation.segment_id or ""),
            "dayId": day_id,
            "amapId": str(poi.id or ""),
            "name": str(poi.name or poi.id or ""),
            "city": str(poi.city or ""),
            "category": str(poi.category or ""),
            "type": str(poi.type or "route_anchor"),
            "source": AMAP_PLACE_SOURCE,
            "longitude": poi.longitude,
            "latitude": poi.latitude,
            "startTime": start_time,
            "endTime": self._format_minutes(self._minutes(start_time) + max(0, int(duration_minutes))),
            "routeOrder": route_order,
            "riskPenaltyMinutes": self._semantic_risk_penalty(operation.semantic_metadata),
            "kind": str(operation.kind or "visit"),
            "semanticMetadata": copy.deepcopy(operation.semantic_metadata or {}),
        }

    def _route_matrix_point_from_snapshot_segment(
        self,
        segment: dict,
        *,
        day_id: str,
        route_order: int,
    ) -> Optional[dict[str, Any]]:
        poi = segment.get("poi") if isinstance(segment.get("poi"), dict) else {}
        try:
            longitude = float(poi.get("longitude"))
            latitude = float(poi.get("latitude"))
        except (TypeError, ValueError):
            return None
        amap_id = str(poi.get("amapId") or poi.get("amap_id") or "")
        if (
            poi.get("source") != AMAP_PLACE_SOURCE
            or not amap_id
            or not math.isfinite(longitude)
            or not math.isfinite(latitude)
        ):
            return None
        return {
            "segmentId": str(segment.get("id") or ""),
            "dayId": day_id,
            "amapId": amap_id,
            "name": str(poi.get("name") or amap_id),
            "city": str(poi.get("city") or ""),
            "category": str(poi.get("category") or ""),
            "type": str(poi.get("type") or "route_anchor"),
            "source": AMAP_PLACE_SOURCE,
            "longitude": longitude,
            "latitude": latitude,
            "startTime": str(segment.get("startTime") or ""),
            "endTime": str(segment.get("endTime") or ""),
            "routeOrder": route_order,
            "riskPenaltyMinutes": self._semantic_risk_penalty(segment.get("semanticMetadata")),
        }

    def _replacement_route_duration(
        self,
        segment: sqlite3.Row,
        operation: ItineraryPatchOperation,
        planning_context: dict,
    ) -> int:
        metadata = self._estimate_metadata(segment)
        duration_metadata = metadata.get("duration") if isinstance(metadata.get("duration"), dict) else {}
        if duration_metadata.get("userLocked"):
            return self._segment_duration(segment)
        poi = operation.amap_poi
        decision = VisitDurationPolicy().normalize_duration(
            None,
            kind=str(segment["kind"] or "visit"),
            category=str(getattr(poi, "category", "") or getattr(poi, "type", "")),
            intent_type=str(getattr(poi, "type", "") or ""),
            context=planning_context,
        )
        return int(decision.preferred_minutes)

    def _route_preflight_transport_mode(
        self,
        plan_id: str,
        operations: list[ItineraryPatchOperation],
        planning_context: dict,
    ) -> Optional[str]:
        contract = self.route_insertion_scorer.normalized_route_decision_contract(
            planning_context.get("routeDecisionContract")
        )
        provenance = contract.get("provenance") if isinstance(contract, dict) else {}
        signed_mode = str(provenance.get("transportMode") or "").strip() if isinstance(provenance, dict) else ""
        operation_modes = {
            str(operation.transport_mode or "").strip()
            for operation in operations
            if str(operation.transport_mode or "").strip()
        }
        if signed_mode:
            if operation_modes and operation_modes != {signed_mode}:
                return None
            return signed_mode

        segment_ids = {str(operation.segment_id) for operation in operations if operation.segment_id}
        day_ids = {
            str(day_id) for operation in operations for day_id in (operation.day_id, operation.target_day_id) if day_id
        }
        snapshot_modes = {
            str(segment.get("transportMode") or "").strip()
            for operation in operations
            if operation.op == "replace_itinerary" and isinstance(operation.full_itinerary, dict)
            for day in operation.full_itinerary.get("days") or []
            if isinstance(day, dict)
            for segment in day.get("segments") or []
            if isinstance(segment, dict) and str(segment.get("transportMode") or "").strip()
        }
        rows: list[sqlite3.Row] = []
        if segment_ids:
            placeholders = ",".join("?" for _ in segment_ids)
            rows.extend(
                self.db.execute(
                    f"SELECT transport_mode FROM itinerary_segments WHERE plan_id = ? AND id IN ({placeholders})",
                    (plan_id, *sorted(segment_ids)),
                ).fetchall()
            )
        if day_ids:
            placeholders = ",".join("?" for _ in day_ids)
            rows.extend(
                self.db.execute(
                    f"SELECT transport_mode FROM itinerary_segments WHERE plan_id = ? AND day_id IN ({placeholders})",
                    (plan_id, *sorted(day_ids)),
                ).fetchall()
            )
        persisted_modes = {
            str(row["transport_mode"] or "").strip() for row in rows if str(row["transport_mode"] or "").strip()
        }
        local_modes = operation_modes | snapshot_modes | persisted_modes
        if len(local_modes) > 1:
            return None
        return next(iter(local_modes), "") or None

    def _route_preflight_tolerance(
        self,
        operation: ItineraryPatchOperation,
        current: Optional[sqlite3.Row],
        planning_context: dict,
    ) -> tuple[Optional[dict[str, float]], str]:
        del operation, current
        contract = self.route_insertion_scorer.normalized_route_decision_contract(
            planning_context.get("routeDecisionContract")
        )
        if contract is None:
            return None, "route_decision_contract_missing_or_invalid"
        return copy.deepcopy(contract["detourTolerance"]), "route_decision_contract"

    @staticmethod
    def _route_preflight_mobility_profile(
        operation: ItineraryPatchOperation,
        planning_context: dict,
    ) -> Optional[dict[str, Any]]:
        del operation
        contract = RouteInsertionScorer.normalized_route_decision_contract(
            planning_context.get("routeDecisionContract")
        )
        return copy.deepcopy(contract["mobilityProfile"]) if contract else None

    @classmethod
    def _adjacent_leg_time_window(
        cls,
        left: dict[str, Any],
        right: dict[str, Any],
        leg: dict[str, Any],
    ) -> tuple[bool, Optional[float]]:
        try:
            available = cls._clock_minutes(right.get("startTime")) - cls._clock_minutes(left.get("endTime"))
            required = float(leg.get("durationSeconds")) / 60
        except (TypeError, ValueError):
            return False, None
        if not math.isfinite(required) or required <= 0 or available < 0:
            return False, None
        return available >= required, max(0.0, available - required)

    @staticmethod
    def _clock_minutes(value: Any) -> int:
        hour, minute = (int(part) for part in str(value).split(":", 1))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("invalid clock")
        return hour * 60 + minute

    @classmethod
    def _point_duration(cls, point: dict[str, Any]) -> int:
        try:
            return max(
                0,
                cls._clock_minutes(point.get("endTime")) - cls._clock_minutes(point.get("startTime")),
            )
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _semantic_risk_penalty(value: Any) -> float:
        if not isinstance(value, dict):
            return 0.0
        try:
            penalty = float(value.get("riskPenaltyMinutes") or 0)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, penalty) if math.isfinite(penalty) else 0.0

    def _route_risk_penalty(self, row: sqlite3.Row) -> float:
        try:
            semantic = json.loads(row["semantic_metadata_json"] or "{}")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            semantic = {}
        return self._semantic_risk_penalty(semantic)

    def _route_insertion_proof_validation_errors(
        self,
        operations: list[ItineraryPatchOperation],
        planning_context: Optional[dict],
        *,
        base_version_id: Optional[str],
    ) -> list[str]:
        expected_candidates = self._expected_route_proof_candidates(operations)
        matrix_required = self._requires_provider_matrix_preflight(operations)
        if not expected_candidates and not matrix_required:
            # Read-only and non-route patches do not inherit or revalidate
            # route evidence from earlier turns.
            return []
        if not isinstance(planning_context, dict) or "routeInsertionProofs" not in planning_context:
            return ["Provider route matrix proof is required before Agent route adoption"]
        expected_contract = self.route_insertion_scorer.normalized_route_decision_contract(
            planning_context.get("routeDecisionContract")
        )
        if expected_contract is None:
            return ["routeDecisionContract is required for every final Provider route decision"]
        raw_proofs = planning_context.get("routeInsertionProofs")
        if not isinstance(raw_proofs, list):
            return ["routeInsertionProofs must be a list"]
        proofs = [item for item in raw_proofs if isinstance(item, dict)]
        identity_proofs = [
            proof
            for proof in proofs
            if str(proof.get("proofType") or "") != "adjacent_route_coverage"
            or str(proof.get("operation") or "") == "replace_itinerary_route_pair"
        ]
        if len(proofs) != len(raw_proofs) or len(identity_proofs) != len(expected_candidates):
            return ["routeInsertionProofs must cover every concrete Agent route decision exactly once"]
        errors: list[str] = []
        expected_coverage_pairs: set[tuple[str, str]] = set()
        if "routeMatrixExpectedPairs" in planning_context:
            raw_pairs = planning_context.get("routeMatrixExpectedPairs")
            if not isinstance(raw_pairs, list):
                errors.append("routeMatrixExpectedPairs must be a list")
            else:
                for pair in raw_pairs:
                    if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                        errors.append("routeMatrixExpectedPairs contains an invalid pair")
                        continue
                    normalized = (str(pair[0] or ""), str(pair[1] or ""))
                    if not all(normalized):
                        errors.append("routeMatrixExpectedPairs contains an invalid pair")
                        continue
                    expected_coverage_pairs.add(normalized)
        covered_pairs: set[tuple[str, str]] = set()
        seen_segments: set[str] = set()
        for proof in proofs:
            proof_contract = self.route_insertion_scorer.normalized_route_decision_contract(
                proof.get("routeDecisionContract")
            )
            if proof_contract is None or proof_contract != expected_contract:
                errors.append(
                    "routeInsertionProofs routeDecisionContract is missing, invalid, or does not match the request/plan contract"
                )
                continue
            segment_id = str(proof.get("segmentId") or "")
            candidate_amap_id = str(proof.get("candidateAmapId") or "")
            proof_type = str(proof.get("proofType") or "")
            operation_name = str(proof.get("operation") or "")
            is_coverage = proof_type == "adjacent_route_coverage"
            is_identity_proof = not is_coverage or (operation_name == "replace_itinerary_route_pair")
            if is_identity_proof:
                if (
                    not segment_id
                    or segment_id in seen_segments
                    or expected_candidates.get(segment_id) != candidate_amap_id
                ):
                    errors.append("routeInsertionProofs candidate identity does not match patch operations")
                    continue
                seen_segments.add(segment_id)
            if str(proof.get("baseVersionId") or "") != str(base_version_id or ""):
                errors.append("routeInsertionProofs baseVersionId does not match the guarded patch base")
            status = str(proof.get("status") or "")
            if status == "not_required":
                if (
                    is_coverage
                    or not is_identity_proof
                    or proof.get("networkRequired") is not False
                    or str(proof.get("proofType") or "") != "route_insertion_or_replacement"
                ):
                    errors.append("routeInsertionProofs cannot waive a required Provider matrix")
                continue
            if (
                status != "passed"
                or proof.get("networkVerified") is not True
                or proof.get("timeWindowFeasible") is not True
                or str(proof.get("detourLevel") or "") == "unacceptable"
            ):
                errors.append("routeInsertionProofs contains a non-acceptable Provider matrix decision")
            legs = proof.get("legs") if isinstance(proof.get("legs"), dict) else {}
            candidate_legs = [
                legs[key] for key in ("previousToCandidate", "candidateToNext") if isinstance(legs.get(key), dict)
            ]
            if not candidate_legs:
                errors.append("routeInsertionProofs is missing candidate Provider legs")
                continue
            if any(not self._provider_matrix_leg_complete_before_write(leg) for leg in candidate_legs):
                errors.append("routeInsertionProofs contains incomplete or stale Provider legs")
                continue
            if not any(
                candidate_amap_id
                in {
                    str(leg.get("fromAmapId") or ""),
                    str(leg.get("toAmapId") or ""),
                }
                for leg in candidate_legs
            ):
                errors.append("routeInsertionProofs candidate identity does not match Provider leg endpoints")
                continue
            if is_coverage:
                if len(candidate_legs) != 1:
                    errors.append("adjacent route coverage proof must contain exactly one Provider leg")
                    continue
                leg = candidate_legs[0]
                pair = (
                    str(leg.get("fromSegmentId") or ""),
                    str(leg.get("toSegmentId") or ""),
                )
                if (
                    not all(pair)
                    or str(proof.get("fromSegmentId") or "") != pair[0]
                    or segment_id != pair[1]
                    or candidate_amap_id != str(leg.get("toAmapId") or "")
                ):
                    errors.append("adjacent route coverage proof does not match its Provider leg")
                    continue
                if operation_name in {
                    "final_route_pair_coverage",
                    "replace_itinerary_route_pair",
                }:
                    covered_pairs.add(pair)
                continue

            baseline = legs.get("previousToNext")
            if len(candidate_legs) == 2:
                if not isinstance(baseline, dict):
                    errors.append("routeInsertionProofs is missing the Provider baseline")
                    continue
                if baseline.get("composite"):
                    components = [
                        legs.get("baselinePreviousToCurrent"),
                        legs.get("baselineCurrentToNext"),
                    ]
                    if any(
                        not isinstance(leg, dict) or not self._provider_matrix_leg_complete_before_write(leg)
                        for leg in components
                    ):
                        errors.append("routeInsertionProofs replacement baseline is incomplete")
                        continue
                elif not self._provider_matrix_leg_complete_before_write(baseline):
                    errors.append("routeInsertionProofs contains an incomplete Provider baseline")
                    continue
            elif operation_name in {
                "replace_segment_poi",
                "replace_segment_poi_from_candidate",
                "replace_poi",
            }:
                if not isinstance(baseline, dict) or not self._provider_matrix_leg_complete_before_write(baseline):
                    errors.append("terminal replacement requires its old Provider-leg baseline")
                    continue
            score = self.route_insertion_scorer.score_from_route_matrix(
                previous_to_candidate=legs.get("previousToCandidate"),
                candidate_to_next=legs.get("candidateToNext"),
                previous_to_next=baseline if isinstance(baseline, dict) else None,
                detour_tolerance=proof_contract["detourTolerance"],
                schedule_slack_minutes=proof.get("scheduleSlackMinutes"),
                time_window_feasible=True,
                mobility_profile=proof_contract["mobilityProfile"],
            )
            if score is None or not score.network_verified or score.detour_level == "unacceptable":
                errors.append("routeInsertionProofs does not reproduce an acceptable Provider matrix decision")
                continue
            try:
                recorded_delta = float(proof.get("generalizedCostDelta"))
            except (TypeError, ValueError):
                errors.append("routeInsertionProofs generalized cost delta is missing")
                continue
            if (
                not math.isfinite(recorded_delta)
                or score.generalized_cost_delta is None
                or abs(recorded_delta - score.generalized_cost_delta) > 0.02
            ):
                errors.append("routeInsertionProofs generalized cost delta does not match its Provider legs")
        if covered_pairs != expected_coverage_pairs:
            errors.append("routeInsertionProofs final Provider coverage does not match the simulated final route pairs")
        return sorted(set(errors))

    def _expected_route_proof_candidates(self, operations: list[ItineraryPatchOperation]) -> dict[str, str]:
        expected: dict[str, str] = {}
        for operation in operations:
            if (
                operation.op
                in {
                    "add_segment",
                    "replace_segment_poi",
                    "replace_segment_poi_from_candidate",
                }
                and operation.segment_id
                and operation.amap_poi is not None
            ):
                expected[str(operation.segment_id)] = str(operation.amap_poi.id or "")
                continue
            if operation.op != "replace_itinerary" or not isinstance(operation.full_itinerary, dict):
                continue
            for day in operation.full_itinerary.get("days") or []:
                if not isinstance(day, dict):
                    continue
                points: list[dict[str, Any]] = []
                potential_count = 0
                for order, segment in enumerate(day.get("segments") or [], start=1):
                    if not isinstance(segment, dict):
                        continue
                    kind = str(segment.get("kind") or "activity")
                    poi = segment.get("poi") if isinstance(segment.get("poi"), dict) else {}
                    semantic = (
                        segment.get("semanticMetadata") if isinstance(segment.get("semanticMetadata"), dict) else {}
                    )
                    route_intent = kind in {"visit", "activity"} or (
                        kind == "meal"
                        and (poi.get("source") == AMAP_PLACE_SOURCE or semantic.get("routeAnchor") is True)
                    )
                    if not route_intent:
                        continue
                    potential_count += 1
                    point = self._route_matrix_point_from_snapshot_segment(
                        segment,
                        day_id=str(day.get("id") or ""),
                        route_order=order,
                    )
                    if point is not None:
                        points.append(point)
                if potential_count < 2 or len(points) != potential_count:
                    continue
                for point in points[1:]:
                    expected[str(point.get("segmentId") or "")] = str(point.get("amapId") or "")
        return expected

    @staticmethod
    def _provider_matrix_leg_complete_before_write(leg: dict[str, Any]) -> bool:
        if str(leg.get("provider") or "") != AMAP_ROUTE_SOURCE:
            return False
        if str(leg.get("source") or AMAP_ROUTE_SOURCE) != AMAP_ROUTE_SOURCE:
            return False
        if not str(leg.get("mode") or "").strip():
            return False
        try:
            distance = float(leg.get("distanceMeters"))
            duration = float(leg.get("durationSeconds"))
            costs = [
                float(leg[key])
                for key in (
                    "walkingDistanceMeters",
                    "transferCount",
                    "waitSeconds",
                    "riskPenaltyMinutes",
                )
            ]
        except (KeyError, TypeError, ValueError):
            return False
        if (
            not math.isfinite(distance)
            or not math.isfinite(duration)
            or distance <= 0
            or duration <= 0
            or any(not math.isfinite(value) or value < 0 for value in costs)
        ):
            return False
        if leg.get("composite"):
            return True
        if not str(leg.get("fromSegmentId") or "") or not str(leg.get("toSegmentId") or ""):
            return False
        if not str(leg.get("fromAmapId") or "") or not str(leg.get("toAmapId") or ""):
            return False
        try:
            queried_at = datetime.fromisoformat(str(leg.get("queriedAt") or ""))
        except ValueError:
            return False
        if queried_at.tzinfo is None or queried_at.utcoffset() is None:
            return False
        age = (datetime.now(timezone.utc) - queried_at.astimezone(timezone.utc)).total_seconds()
        return -60 <= age <= ROUTE_CACHE_TTL_SECONDS

    def _persist_provider_matrix_routes(
        self,
        plan_id: str,
        planning_context: Optional[dict],
    ) -> None:
        """Materialize verified candidate legs before the first version save.

        The proof and selected route then share identical endpoints, metrics,
        and query time.  No post-write Provider retry is needed to make the
        newly adopted snapshot pass its basic route-coverage verifier.
        """
        proofs = self._route_insertion_proofs(planning_context)
        candidate_legs: dict[tuple[str, str], dict[str, Any]] = {}
        for proof in proofs:
            if str(proof.get("status") or "") != "passed":
                continue
            legs = proof.get("legs") if isinstance(proof.get("legs"), dict) else {}
            for key in ("previousToCandidate", "candidateToNext"):
                leg = legs.get(key)
                if not isinstance(leg, dict):
                    continue
                pair = (
                    str(leg.get("fromSegmentId") or ""),
                    str(leg.get("toSegmentId") or ""),
                )
                if all(pair):
                    candidate_legs[pair] = leg
        if not candidate_legs:
            return
        itinerary_service = ItineraryService(self.db)
        itinerary_service._delete_routes(plan_id, set(candidate_legs))
        for (from_segment_id, to_segment_id), leg in sorted(candidate_legs.items()):
            if not self._provider_matrix_leg_complete_before_write(leg):
                raise HTTPException(
                    status_code=409,
                    detail="Provider route matrix leg became invalid before route materialization",
                )
            endpoints = self.db.execute(
                """
                SELECT
                    from_segment.poi_id AS from_poi_id,
                    to_segment.poi_id AS to_poi_id,
                    from_poi.amap_id AS from_amap_id,
                    to_poi.amap_id AS to_amap_id
                FROM itinerary_segments from_segment
                JOIN itinerary_segments to_segment
                  ON to_segment.id = ? AND to_segment.plan_id = ?
                JOIN pois from_poi ON from_poi.id = from_segment.poi_id
                JOIN pois to_poi ON to_poi.id = to_segment.poi_id
                WHERE from_segment.id = ? AND from_segment.plan_id = ?
                """,
                (to_segment_id, plan_id, from_segment_id, plan_id),
            ).fetchone()
            if (
                endpoints is None
                or str(endpoints["from_amap_id"] or "") != str(leg.get("fromAmapId") or "")
                or str(endpoints["to_amap_id"] or "") != str(leg.get("toAmapId") or "")
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Provider route matrix endpoints changed before route materialization",
                )
            queried_at = datetime.fromisoformat(str(leg.get("queriedAt") or ""))
            if queried_at.tzinfo is None or queried_at.utcoffset() is None:
                raise HTTPException(
                    status_code=409,
                    detail="Provider route matrix query time became invalid before route materialization",
                )
            provider_payload = (
                copy.deepcopy(leg.get("providerPayload")) if isinstance(leg.get("providerPayload"), dict) else {}
            )
            provider_payload.update(
                {
                    "routeMatrixProofMaterialized": True,
                    "walkingDistanceMeters": float(leg.get("walkingDistanceMeters") or 0),
                    "transferCount": float(leg.get("transferCount") or 0),
                    "waitSeconds": float(leg.get("waitSeconds") or 0),
                    "riskPenaltyMinutes": float(leg.get("riskPenaltyMinutes") or 0),
                    "costComponentProvenance": copy.deepcopy(leg.get("costComponentProvenance") or {}),
                }
            )
            itinerary_service._insert_route(
                RouteOption(
                    id=f"route_matrix_{uuid4().hex[:12]}",
                    plan_id=plan_id,
                    from_segment_id=from_segment_id,
                    to_segment_id=to_segment_id,
                    from_poi_id=str(endpoints["from_poi_id"] or ""),
                    to_poi_id=str(endpoints["to_poi_id"] or ""),
                    provider=AMAP_ROUTE_SOURCE,
                    mode=str(leg["mode"]).strip(),
                    is_selected=True,
                    sort_order=1,
                    distance_meters=int(round(float(leg["distanceMeters"]))),
                    duration_seconds=int(round(float(leg["durationSeconds"]))),
                    cost_amount=float(leg.get("costAmount") or 0),
                    cost_currency=str(leg.get("costCurrency") or "CNY"),
                    polyline=(copy.deepcopy(leg.get("polyline")) if isinstance(leg.get("polyline"), list) else []),
                    steps=(copy.deepcopy(leg.get("steps")) if isinstance(leg.get("steps"), list) else []),
                    provider_payload=provider_payload,
                    queried_at=queried_at,
                )
            )

    def select_route(
        self,
        plan_id: str,
        route_option_id: str,
        base_version_id: Optional[str] = None,
        planning_context: Optional[dict] = None,
    ) -> ItineraryPatchResponse:
        session = self._session_for_plan(plan_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Conversation session for itinerary plan not found")
        ensure_base_version_current(base_version_id, session["active_version_id"])

        route = self.db.execute(
            "SELECT * FROM route_options WHERE id = ? AND plan_id = ?",
            (route_option_id, plan_id),
        ).fetchone()
        if route is None:
            raise HTTPException(status_code=404, detail="Route option not found for this itinerary")
        if route["error_json"]:
            raise HTTPException(status_code=400, detail="Cannot select an unavailable route option")
        if not route["polyline_json"] or route["polyline_json"] == "[]":
            raise HTTPException(status_code=400, detail="Cannot select a route option without AMap polyline")

        route_decision_contract = self._active_server_route_decision_contract(
            plan_id=plan_id,
            active_version_id=str(session["active_version_id"] or ""),
            session_id=str(session["id"]),
        )
        patch_id = f"patch_{uuid4().hex[:12]}"
        operation = {
            "op": "select_route",
            "routeOptionId": route_option_id,
            "fromSegmentId": route["from_segment_id"],
            "toSegmentId": route["to_segment_id"],
        }
        route_group = self._route_group_context(planning_context)
        if route_group:
            operation["routeGroup"] = route_group
        savepoint = f"route_select_{uuid4().hex}"
        self.db.execute(f"SAVEPOINT {savepoint}")
        try:
            baseline_selected_rows = self._selected_route_rows_snapshot(plan_id)
            self.db.execute(
                """
                UPDATE route_options
                SET is_selected = 0
                WHERE plan_id = ?
                  AND COALESCE(from_segment_id, '') = COALESCE(?, '')
                  AND COALESCE(to_segment_id, '') = COALESCE(?, '')
                  AND from_poi_id = ?
                  AND to_poi_id = ?
                """,
                (
                    plan_id,
                    route["from_segment_id"],
                    route["to_segment_id"],
                    route["from_poi_id"],
                    route["to_poi_id"],
                ),
            )
            self.db.execute(
                "UPDATE route_options SET is_selected = 1 WHERE id = ? AND plan_id = ?",
                (route_option_id, plan_id),
            )
            schedule_updated_count = ItineraryScheduleService(self.db).recompute_after_route_selection(
                plan_id,
                route_option_id,
            )
            snapshot_service = ItinerarySnapshotService(self.db)
            snapshot = snapshot_service.capture_snapshot(plan_id)
            self._rebuild_selected_route_matrix_snapshot(
                plan_id,
                snapshot,
                baseline_selected_rows=baseline_selected_rows,
                route_decision_contract=route_decision_contract,
                base_version_id=base_version_id,
                operation="select_route_final_route_pair",
            )
            self.db.execute(
                """
                INSERT INTO itinerary_patches (
                    id, session_id, plan_id, base_version_id, result_version_id,
                    source_type, source_turn_id, planning_run_id, operations_json, validation_status,
                    validation_errors_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    patch_id,
                    session["id"],
                    plan_id,
                    base_version_id,
                    None,
                    "manual",
                    None,
                    None,
                    json.dumps([operation], ensure_ascii=False),
                    "accepted",
                    "[]",
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            version = snapshot_service.save_version(
                session["id"],
                plan_id,
                "manual",
                snapshot=snapshot,
                source_patch_id=patch_id,
            )
            self.db.execute(
                "UPDATE itinerary_patches SET result_version_id = ? WHERE id = ?",
                (version.id, patch_id),
            )
        except Exception:
            self.db.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            self.db.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise
        self.db.execute(f"RELEASE SAVEPOINT {savepoint}")
        self.db.commit()
        return ItineraryPatchResponse(
            itinerary=ItineraryService(self.db).get_plan(plan_id),
            patch=ItineraryPatchSummaryResponse(
                id=patch_id,
                validation_status="accepted",
                metadata={
                    "schedulePolicy": "computed_from_selected_routes",
                    "scheduleUpdatedCount": schedule_updated_count,
                    **({"routeGroup": route_group} if route_group else {}),
                },
            ),
            version=version,
            validation_errors=[],
            pending_poi_candidates=self._pending_candidates_payload(session["id"]),
        )

    def optimize_routes(
        self,
        plan_id: str,
        *,
        base_version_id: Optional[str] = None,
        source_type: str = "manual",
        source_turn_id: Optional[str] = None,
        preference_summary: Optional[str] = None,
        planning_context: Optional[dict] = None,
        day_id: Optional[str] = None,
        optimization_objective: str = "balanced",
    ) -> ItineraryPatchResponse:
        self._assert_patch_deadline(planning_context)
        session = self._session_for_plan(plan_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Conversation session for itinerary plan not found")
        ensure_base_version_current(base_version_id, session["active_version_id"])
        if source_type == "agent":
            # RouteOptimizationService may update route selections internally,
            # so the Agent run must cross its write fence before entering it.
            begin_session_run_write(str(session["id"]))

        route_decision_contract = self._active_server_route_decision_contract(
            plan_id=plan_id,
            active_version_id=str(session["active_version_id"] or ""),
            session_id=str(session["id"]),
        )
        schedule_only = (
            bool((planning_context or {}).get("scheduleOnly")) if isinstance(planning_context, dict) else False
        )
        patch_id = f"patch_{uuid4().hex[:12]}"
        savepoint = f"route_optimize_{uuid4().hex}"
        self.db.execute(f"SAVEPOINT {savepoint}")
        try:
            baseline_selected_rows = self._selected_route_rows_snapshot(plan_id)
            result = (
                RouteOptimizationResult()
                if schedule_only
                else RouteOptimizationService(self.db).optimize_plan_routes(
                    plan_id,
                    day_id=day_id,
                    objective=optimization_objective,
                    preference={
                        "preferredMode": self._preferred_mode_from_context(
                            preference_summary,
                            planning_context,
                        ),
                        "preferenceSummary": preference_summary or "",
                    },
                )
            )
            schedule_updated_count = 0
            if schedule_only:
                schedule_updated_count = ItineraryScheduleService(self.db).recompute_plan_schedule(plan_id)
            elif result.changed_count:
                changed_pairs = {
                    (change.from_segment_id, change.to_segment_id)
                    for change in result.changes
                    if change.from_segment_id and change.to_segment_id
                }
                schedule_updated_count = ItineraryScheduleService(self.db).recompute_plan_schedule(
                    plan_id,
                    route_pairs=changed_pairs or None,
                )
            operation = {
                "op": "auto_schedule" if schedule_only else "optimize_routes",
                "scope": "day" if day_id else "plan",
                "dayId": day_id,
                "result": result.as_metadata(),
                "schedulePolicy": "computed_from_selected_routes",
                "scheduleUpdatedCount": schedule_updated_count,
            }
            snapshot_service = ItinerarySnapshotService(self.db)
            snapshot = snapshot_service.capture_snapshot(plan_id)
            self._rebuild_selected_route_matrix_snapshot(
                plan_id,
                snapshot,
                baseline_selected_rows=baseline_selected_rows,
                route_decision_contract=route_decision_contract,
                base_version_id=base_version_id,
                operation=("auto_schedule_final_route_pair" if schedule_only else "optimize_routes_final_route_pair"),
            )
            self.db.execute(
                """
                INSERT INTO itinerary_patches (
                    id, session_id, plan_id, base_version_id, result_version_id,
                    source_type, source_turn_id, planning_run_id, operations_json, validation_status,
                    validation_errors_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    patch_id,
                    session["id"],
                    plan_id,
                    base_version_id,
                    None,
                    source_type,
                    source_turn_id,
                    None,
                    json.dumps([operation], ensure_ascii=False),
                    "accepted",
                    "[]",
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            version = snapshot_service.save_version(
                session["id"],
                plan_id,
                source_type,
                snapshot=snapshot,
                source_patch_id=patch_id,
            )
            self.db.execute(
                "UPDATE itinerary_patches SET result_version_id = ? WHERE id = ?",
                (version.id, patch_id),
            )
        except Exception:
            self.db.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            self.db.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise
        self.db.execute(f"RELEASE SAVEPOINT {savepoint}")
        self.db.commit()
        return ItineraryPatchResponse(
            itinerary=ItineraryService(self.db).get_plan(plan_id),
            patch=ItineraryPatchSummaryResponse(
                id=patch_id,
                validation_status="accepted",
                metadata={
                    "routeOptimization": result.as_metadata(),
                    "schedulePolicy": "computed_from_selected_routes",
                    "scheduleUpdatedCount": schedule_updated_count,
                },
            ),
            version=version,
            validation_errors=[],
            pending_poi_candidates=self._pending_candidates_payload(session["id"]),
        )

    def _route_group_context(self, planning_context: Optional[dict]) -> dict:
        if not isinstance(planning_context, dict):
            return {}
        route_group = planning_context.get("routeGroup")
        if not isinstance(route_group, dict):
            route_group = (
                planning_context.get("selectedRouteGroup")
                if isinstance(planning_context.get("selectedRouteGroup"), dict)
                else {}
            )
        if not isinstance(route_group, dict) or not route_group:
            return {}
        raw_route_ids = route_group.get("rawRouteIds") or route_group.get("routeIds") or []
        return {
            "label": str(route_group.get("label") or "").strip(),
            "rawRouteIds": [str(route_id) for route_id in raw_route_ids if route_id],
            "selectedRawMode": str(route_group.get("selectedRawMode") or "").strip(),
            "representativeRouteId": str(route_group.get("representativeRouteId") or "").strip(),
        }

    def _preferred_mode_from_context(self, preference_summary: Optional[str], planning_context: Optional[dict]) -> str:
        context = planning_context if isinstance(planning_context, dict) else {}
        explicit = str(context.get("preferredRouteMode") or context.get("preferredMode") or "").strip()
        if explicit:
            return explicit
        text = f"{preference_summary or ''} {context.get('currentPreferenceSummary') or ''}"
        if re.search(r"公交|地铁|公共交通", text):
            return "transit"
        if re.search(r"驾车|自驾", text):
            return "driving"
        if re.search(r"打车|出租", text):
            return "taxi"
        if re.search(r"步行|徒步", text):
            return "walking"
        return ""

    def _pending_candidates_payload(self, session_id: str) -> list[dict]:
        rows = self.db.execute(
            """
            SELECT * FROM amap_poi_candidates
            WHERE session_id = ? AND status = 'pending'
            ORDER BY created_at ASC
            """,
            (session_id,),
        ).fetchall()
        payload = []
        for row in rows:
            try:
                candidates = json.loads(row["candidates_json"] or "[]")
            except json.JSONDecodeError:
                candidates = []
            payload.append(
                {
                    "id": row["id"],
                    "query": row["query"],
                    "city": row["city"],
                    "category": row["category"],
                    "status": row["status"],
                    "candidates": candidates,
                    "selectedAmapId": row["selected_amap_id"],
                    "sourceSegmentId": row["segment_id"] if "segment_id" in row.keys() else None,
                    "createdAt": row["created_at"],
                }
            )
        return payload

    def _http_error_message(self, error: HTTPException) -> str:
        detail = error.detail
        if isinstance(detail, str):
            return detail
        return json.dumps(detail, ensure_ascii=False)

    def attach_planning_run(self, patch_id: str, planning_run_id: str) -> None:
        self.db.execute(
            "UPDATE itinerary_patches SET planning_run_id = ? WHERE id = ?",
            (planning_run_id, patch_id),
        )

    def attach_planning_run_for_source_turn(self, source_turn_id: str, planning_run_id: str) -> None:
        self.db.execute(
            """
            UPDATE itinerary_patches
            SET planning_run_id = ?
            WHERE source_turn_id = ?
              AND planning_run_id IS NULL
            """,
            (planning_run_id, source_turn_id),
        )

    def _session_for_plan(self, plan_id: str) -> Optional[sqlite3.Row]:
        session = self.db.execute(
            "SELECT * FROM conversation_sessions WHERE active_plan_id = ?",
            (plan_id,),
        ).fetchone()
        if session is not None:
            return session

        plan = self.db.execute("SELECT * FROM itinerary_plans WHERE id = ?", (plan_id,)).fetchone()
        if plan is None:
            return None
        now = datetime.now(timezone.utc).isoformat()
        session_id = f"sess_{uuid4().hex[:12]}"
        self.db.execute(
            """
            INSERT INTO conversation_sessions (
                id, user_id, title, city, active_plan_id, active_version_id,
                status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                plan["user_id"],
                plan["title"],
                plan["city"],
                plan_id,
                None,
                "active",
                now,
                now,
            ),
        )
        return self.db.execute("SELECT * FROM conversation_sessions WHERE id = ?", (session_id,)).fetchone()

    def _validate(
        self,
        plan_id: str,
        operations: list[ItineraryPatchOperation],
        *,
        planning_context: Optional[dict] = None,
        allow_server_sealed_zero_target_days: bool = False,
    ) -> list[str]:
        errors = []
        if not operations:
            return ["At least one itinerary patch operation is required"]
        new_day_available = False
        allow_soft_pending_days = bool(
            isinstance(planning_context, dict)
            and planning_context.get("portfolioCommit") is True
            and planning_context.get("portfolioDraftAdoptionReady") is True
            and planning_context.get("portfolioAdoptionMode") in {"editable_draft", "editable_partial"}
        )
        for operation in operations:
            if operation.op == "replace_itinerary":
                errors.extend(
                    self._validate_replacement_snapshot(
                        plan_id,
                        operation.full_itinerary,
                        allow_soft_pending_days=allow_soft_pending_days,
                        allow_server_sealed_zero_target_days=allow_server_sealed_zero_target_days,
                    )
                )
                continue
            if operation.op == "replace_trip_title":
                if not (operation.value or "").strip():
                    errors.append("Trip title cannot be empty")
                continue
            if operation.op == "replace_day_title":
                if not operation.day_id:
                    errors.append("replace_day_title requires dayId")
                    continue
                if not (operation.value or "").strip():
                    errors.append("Day title cannot be empty")
                elif not self._day_exists(plan_id, operation.day_id):
                    errors.append(f"Day not found: {operation.day_id}")
                continue
            if operation.op == "replace_segment_start_time":
                if not operation.segment_id:
                    errors.append("replace_segment_start_time requires segmentId")
                    continue
                next_start = operation.start_time or operation.value or ""
                if not self._valid_clock(next_start):
                    errors.append("Segment start time must use HH:mm format")
                    continue
                segment = self._segment(plan_id, operation.segment_id)
                if segment is None:
                    errors.append(f"Segment not found: {operation.segment_id}")
                    continue
                time_error = self._validate_segment_time(plan_id, segment, next_start)
                if time_error:
                    errors.append(time_error)
                continue
            if operation.op == "replace_segment_duration":
                if not operation.segment_id:
                    errors.append("replace_segment_duration requires segmentId")
                    continue
                if operation.duration_minutes is None or operation.duration_minutes <= 0:
                    errors.append("Segment duration must be a positive number of minutes")
                    continue
                segment = self._segment(plan_id, operation.segment_id)
                if segment is None:
                    errors.append(f"Segment not found: {operation.segment_id}")
                    continue
                interval_error = self._validate_interval(
                    plan_id,
                    segment["day_id"],
                    segment["start_time"],
                    operation.duration_minutes,
                    ignore_segment_id=segment["id"],
                )
                if interval_error:
                    errors.append(interval_error)
                continue
            if operation.op == "replace_transport_mode":
                if not operation.segment_id:
                    errors.append("replace_transport_mode requires segmentId")
                    continue
                if not (operation.value or "").strip():
                    errors.append("Transport mode cannot be empty")
                    continue
                if self._segment(plan_id, operation.segment_id) is None:
                    errors.append(f"Segment not found: {operation.segment_id}")
                continue
            if operation.op == "add_day":
                title = operation.title or operation.value or "待规划日程"
                if not title.strip():
                    errors.append("Day title cannot be empty")
                new_day_available = True
                continue
            if operation.op == "add_segment":
                if not operation.day_id:
                    errors.append("add_segment requires dayId")
                    continue
                if not self._day_exists(plan_id, operation.day_id):
                    errors.append(f"Day not found: {operation.day_id}")
                    continue
                if operation.amap_poi is None and not self._allows_unresolved_add_segment(operation):
                    errors.append("add_segment requires resolved AMap POI")
                    continue
                if operation.amap_poi is not None:
                    poi_error = self._validate_resolved_amap_poi(operation.amap_poi, "add_segment")
                    if poi_error:
                        errors.append(poi_error)
                        continue
                    if self._meal_operation_uses_non_food(plan_id, operation):
                        errors.append("Meal segments must use a food-service AMap POI, not a shopping complex")
                        continue
                start_time = (
                    operation.start_time or operation.value or self._next_segment_start_time(plan_id, operation.day_id)
                )
                if not self._valid_clock(start_time):
                    errors.append("Segment start time must use HH:mm format")
                    continue
                duration = operation.duration_minutes or 30
                interval_error = self._validate_interval(plan_id, operation.day_id, start_time, duration)
                if interval_error:
                    errors.append(interval_error)
                continue
            if operation.op == "reorder_segments":
                if not operation.day_id:
                    errors.append("reorder_segments requires dayId")
                    continue
                if not self._day_exists(plan_id, operation.day_id):
                    errors.append(f"Day not found: {operation.day_id}")
                    continue
                errors.extend(
                    self._validate_reorder_segments(plan_id, operation.day_id, operation.ordered_segment_ids or [])
                )
                continue
            if operation.op == "replace_segment_poi_from_candidate":
                if not operation.segment_id:
                    errors.append("replace_segment_poi_from_candidate requires segmentId")
                    continue
                if not operation.candidate_id:
                    errors.append("replace_segment_poi_from_candidate requires candidateId")
                    continue
                if self._segment(plan_id, operation.segment_id) is None:
                    errors.append(f"Segment not found: {operation.segment_id}")
                    continue
                poi_error = self._validate_resolved_amap_poi(operation.amap_poi, operation.op)
                if poi_error:
                    errors.append(poi_error)
                elif self._meal_operation_uses_non_food(plan_id, operation):
                    errors.append("Meal segments must use a food-service AMap POI, not a shopping complex")
                continue
            if operation.op == "replace_segment_poi":
                if not operation.segment_id:
                    errors.append("replace_segment_poi requires segmentId")
                    continue
                if self._segment(plan_id, operation.segment_id) is None:
                    errors.append(f"Segment not found: {operation.segment_id}")
                    continue
                poi_error = self._validate_resolved_amap_poi(operation.amap_poi, operation.op)
                if poi_error:
                    errors.append(poi_error)
                elif self._meal_operation_uses_non_food(plan_id, operation):
                    errors.append("Meal segments must use a food-service AMap POI, not a shopping complex")
                continue
            if operation.op == "expand_area_poi_candidates":
                if not operation.segment_id:
                    errors.append("expand_area_poi_candidates requires segmentId")
                    continue
                segment = self._segment_with_poi(plan_id, operation.segment_id)
                if segment is None:
                    errors.append(f"Segment not found: {operation.segment_id}")
                    continue
                metadata = self._poi_grounding_metadata(segment)
                if not metadata.get("needsConcretePoi"):
                    errors.append(
                        "expand_area_poi_candidates only supports area/function/composite POIs that need a concrete target"
                    )
                if operation.radius is not None and (operation.radius < 50 or operation.radius > 5000):
                    errors.append("expand_area_poi_candidates radius must be between 50 and 5000 meters")
                continue
            if operation.op == "expand_meal_poi_candidates":
                if not operation.segment_id:
                    errors.append("expand_meal_poi_candidates requires segmentId")
                    continue
                segment = self._segment_with_poi(plan_id, operation.segment_id)
                if segment is None:
                    errors.append(f"Segment not found: {operation.segment_id}")
                    continue
                if segment["kind"] != "meal":
                    errors.append("expand_meal_poi_candidates only supports meal segments")
                    continue
                metadata = self._poi_grounding_metadata(segment)
                if not self._meal_segment_allows_candidate_expansion(segment, metadata):
                    errors.append("expand_meal_poi_candidates only supports ungrounded or pending meal segments")
                if operation.radius is not None and (operation.radius < 50 or operation.radius > 5000):
                    errors.append("expand_meal_poi_candidates radius must be between 50 and 5000 meters")
                continue
            if operation.op == "confirm_poi_anchor":
                if not operation.segment_id:
                    errors.append("confirm_poi_anchor requires segmentId")
                    continue
                segment = self._segment_with_poi(plan_id, operation.segment_id)
                if segment is None:
                    errors.append(f"Segment not found: {operation.segment_id}")
                    continue
                if not segment["amap_id"] or segment["latitude"] is None or segment["longitude"] is None:
                    errors.append("confirm_poi_anchor requires an existing routeable AMap anchor")
                    continue
                metadata = self._poi_grounding_metadata(segment)
                if metadata["groundingStatus"] != "routeable_anchor":
                    errors.append(
                        "confirm_poi_anchor only supports exact routeable anchors; area/function POIs must be replaced with a concrete AMap candidate first"
                    )
                continue
            if operation.op == "refresh_ticket_for_segment":
                if not operation.segment_id:
                    errors.append("refresh_ticket_for_segment requires segmentId")
                elif self._segment(plan_id, operation.segment_id) is None:
                    errors.append(f"Segment not found: {operation.segment_id}")
                continue
            if operation.op == "refresh_routes_for_day":
                if not operation.day_id:
                    errors.append("refresh_routes_for_day requires dayId")
                elif not self._day_exists(plan_id, operation.day_id):
                    errors.append(f"Day not found: {operation.day_id}")
                continue
            if operation.op == "remove_segment":
                if not operation.segment_id:
                    errors.append("remove_segment requires segmentId")
                elif self._segment(plan_id, operation.segment_id) is None:
                    errors.append(f"Segment not found: {operation.segment_id}")
                continue
            if operation.op == "move_segment":
                if not operation.segment_id:
                    errors.append("move_segment requires segmentId")
                    continue
                segment = self._segment(plan_id, operation.segment_id)
                if segment is None:
                    errors.append(f"Segment not found: {operation.segment_id}")
                    continue
                target_day_id = operation.target_day_id or operation.day_id
                if not target_day_id:
                    errors.append("move_segment requires targetDayId")
                    continue
                if target_day_id == NEW_DAY_TARGET_ID:
                    if not new_day_available:
                        errors.append("move_segment targetDayId __new_day__ requires a preceding add_day")
                        continue
                elif not self._day_exists(plan_id, target_day_id):
                    errors.append(f"Target day not found: {target_day_id}")
                    continue
                start_time = operation.start_time or operation.value or segment["start_time"]
                if not self._valid_clock(start_time):
                    errors.append("Segment start time must use HH:mm format")
                    continue
                duration = self._segment_duration(segment)
                if target_day_id == NEW_DAY_TARGET_ID:
                    if duration <= 0 or self._minutes(start_time) + duration > 24 * 60:
                        errors.append("Segment end time cannot cross days")
                else:
                    interval_error = self._validate_interval(
                        plan_id,
                        target_day_id,
                        start_time,
                        duration,
                        ignore_segment_id=segment["id"],
                    )
                    if interval_error:
                        errors.append(interval_error)
                continue
            if operation.op == "update_segment_notes":
                if not operation.segment_id:
                    errors.append("update_segment_notes requires segmentId")
                elif self._segment(plan_id, operation.segment_id) is None:
                    errors.append(f"Segment not found: {operation.segment_id}")
                elif not ((operation.notes if operation.notes is not None else operation.value) or "").strip():
                    errors.append("Segment notes cannot be empty")
                continue
            errors.append(f"Unsupported itinerary patch operation: {operation.op}")
        return errors

    def _apply_operations(
        self,
        plan_id: str,
        operations: list[ItineraryPatchOperation],
        prepared_expansions: Optional[dict[str, dict]] = None,
        source_type: str = "manual",
        planning_context: Optional[dict] = None,
    ) -> None:
        prepared_expansions = prepared_expansions or {}
        now = datetime.now(timezone.utc).isoformat()
        new_day_id: Optional[str] = None
        for operation in operations:
            if operation.op == "replace_itinerary":
                ItinerarySnapshotService(self.db).apply_snapshot(
                    plan_id,
                    self._normalize_replacement_snapshot(plan_id, operation.full_itinerary or {}),
                )
            elif operation.op == "replace_trip_title":
                self.db.execute(
                    "UPDATE itinerary_plans SET title = ?, updated_at = ? WHERE id = ?",
                    ((operation.value or "").strip(), now, plan_id),
                )
            elif operation.op == "replace_day_title":
                self.db.execute(
                    "UPDATE itinerary_days SET title = ? WHERE id = ? AND plan_id = ?",
                    ((operation.value or "").strip(), operation.day_id, plan_id),
                )
            elif operation.op == "replace_segment_start_time":
                segment = self._segment(plan_id, operation.segment_id or "")
                duration = self._segment_duration(segment)
                next_start = operation.start_time or operation.value or ""
                next_end = self._format_minutes(self._minutes(next_start) + duration)
                metadata = self._estimate_metadata(segment)
                duration_metadata = metadata.get("duration") if isinstance(metadata.get("duration"), dict) else {}
                duration_metadata.update({"userLocked": True, "source": "user_locked"})
                metadata["duration"] = duration_metadata
                semantic_metadata = self._row_semantic_metadata(segment)
                if source_type == "user_timeline_mutation":
                    # Keep preflight and final write on the same trusted
                    # server-side schedule overlay.
                    semantic_metadata = self._semantic_metadata_for_user_start_time(
                        segment,
                        start_time=next_start,
                        duration_minutes=duration,
                    )
                self.db.execute(
                    "UPDATE itinerary_segments SET start_time = ?, end_time = ?, estimate_metadata_json = ?, "
                    "semantic_metadata_json = ? WHERE id = ? AND plan_id = ?",
                    (
                        next_start,
                        next_end,
                        json.dumps(metadata, ensure_ascii=False),
                        json.dumps(semantic_metadata, ensure_ascii=False),
                        operation.segment_id,
                        plan_id,
                    ),
                )
            elif operation.op == "replace_segment_duration":
                segment = self._segment(plan_id, operation.segment_id or "")
                duration = int(operation.duration_minutes or 0)
                metadata = self._estimate_metadata(segment)
                duration_metadata = metadata.get("duration") if isinstance(metadata.get("duration"), dict) else {}
                duration_metadata.update({"userLocked": True, "source": "user_locked", "minutes": duration})
                metadata["duration"] = duration_metadata
                self.db.execute(
                    "UPDATE itinerary_segments SET end_time = ?, estimate_metadata_json = ? WHERE id = ? AND plan_id = ?",
                    (
                        self._format_minutes(self._minutes(segment["start_time"]) + duration),
                        json.dumps(metadata, ensure_ascii=False),
                        operation.segment_id,
                        plan_id,
                    ),
                )
            elif operation.op == "replace_transport_mode":
                self.db.execute(
                    "UPDATE itinerary_segments SET transport_mode = ? WHERE id = ? AND plan_id = ?",
                    ((operation.value or "").strip(), operation.segment_id, plan_id),
                )
            elif operation.op == "add_day":
                new_day_id = self._add_day(plan_id, operation.title or operation.value or "待规划日程")
            elif operation.op == "add_segment":
                self._add_segment(plan_id, operation, source_type=source_type)
            elif operation.op == "reorder_segments":
                self._reorder_segments(plan_id, operation.day_id or "", operation.ordered_segment_ids or [])
            elif operation.op in {"replace_segment_poi", "replace_segment_poi_from_candidate"}:
                self._replace_segment_poi(
                    plan_id,
                    operation,
                    source_type=source_type,
                    planning_context=planning_context,
                )
            elif operation.op == "expand_area_poi_candidates":
                self._expand_area_poi_candidates(plan_id, operation, prepared_expansions)
            elif operation.op == "expand_meal_poi_candidates":
                self._expand_meal_poi_candidates(plan_id, operation, prepared_expansions)
            elif operation.op == "confirm_poi_anchor":
                self._confirm_poi_anchor(plan_id, operation.segment_id or "")
            elif operation.op == "refresh_ticket_for_segment":
                pass
            elif operation.op == "refresh_routes_for_day":
                pass
            elif operation.op == "remove_segment":
                segment = self._segment(plan_id, operation.segment_id or "")
                poi_id = segment["poi_id"] if segment is not None else None
                self.db.execute(
                    "DELETE FROM itinerary_segments WHERE id = ? AND plan_id = ?",
                    (operation.segment_id, plan_id),
                )
                self._delete_routes_touching_removed_segment(plan_id, operation.segment_id or "", poi_id)
                self._reorder_day_segments(plan_id, segment["day_id"])
            elif operation.op == "move_segment":
                effective_operation = operation
                if (operation.target_day_id or operation.day_id) == NEW_DAY_TARGET_ID:
                    if not new_day_id:
                        raise HTTPException(status_code=400, detail="New day placeholder was not resolved")
                    effective_operation = operation.model_copy(update={"target_day_id": new_day_id})
                self._move_segment(plan_id, effective_operation)
            elif operation.op == "update_segment_notes":
                self.db.execute(
                    "UPDATE itinerary_segments SET notes = ? WHERE id = ? AND plan_id = ?",
                    (
                        (operation.notes if operation.notes is not None else operation.value) or "",
                        operation.segment_id,
                        plan_id,
                    ),
                )
        self._recalculate_plan_totals(plan_id)

    def _poi_grounding_metadata(self, row: sqlite3.Row) -> dict:
        return ItineraryService(self.db)._poi_grounding_metadata_from_values(
            source=row["source"],
            amap_id=row["amap_id"],
            longitude=row["longitude"],
            latitude=row["latitude"],
            confidence=row["confidence"],
            name=row["poi_name"],
            source_note=row["source_note"],
        )

    def _ticket_invalidation_segment_ids(self, operations: list[ItineraryPatchOperation]) -> list[str]:
        seen: set[str] = set()
        segment_ids: list[str] = []
        for operation in operations:
            if (
                operation.op in {"replace_segment_poi", "replace_segment_poi_from_candidate", "remove_segment"}
                and operation.segment_id
            ):
                if operation.segment_id not in seen:
                    seen.add(operation.segment_id)
                    segment_ids.append(operation.segment_id)
        return segment_ids

    def _invalidate_stale_ticket_results(self, plan_id: str, segment_ids: list[str]) -> None:
        if not segment_ids:
            return
        placeholders = ",".join("?" for _ in segment_ids)
        self.db.execute(f"DELETE FROM ticket_lookup_results WHERE segment_id IN ({placeholders})", segment_ids)
        self.db.execute(
            f"UPDATE itinerary_segments SET ticket_lookup_result_id = NULL WHERE plan_id = ? AND id IN ({placeholders})",
            [plan_id, *segment_ids],
        )

    def _delete_routes_touching_removed_segment(self, plan_id: str, segment_id: str, poi_id: Optional[str]) -> None:
        if not segment_id:
            return
        clauses = ["from_segment_id = ?", "to_segment_id = ?"]
        params: list[str] = [segment_id, segment_id]
        if poi_id:
            remaining_poi_use = self.db.execute(
                "SELECT 1 FROM itinerary_segments WHERE plan_id = ? AND poi_id = ? LIMIT 1",
                (plan_id, poi_id),
            ).fetchone()
            if remaining_poi_use is None:
                clauses.extend(["from_poi_id = ?", "to_poi_id = ?"])
                params.extend([poi_id, poi_id])
        rows = self.db.execute(
            f"SELECT id FROM route_options WHERE plan_id = ? AND ({' OR '.join(clauses)})",
            [plan_id, *params],
        ).fetchall()
        route_ids = [row["id"] for row in rows]
        if not route_ids:
            return
        placeholders = ",".join("?" for _ in route_ids)
        self.db.execute(f"DELETE FROM traffic_crowding_signals WHERE route_option_id IN ({placeholders})", route_ids)
        self.db.execute(
            f"DELETE FROM route_options WHERE plan_id = ? AND id IN ({placeholders})", [plan_id, *route_ids]
        )

    def _ticket_refresh_segment_ids(self, operations: list[ItineraryPatchOperation]) -> list[str]:
        seen: set[str] = set()
        segment_ids: list[str] = []
        for operation in operations:
            if (
                operation.op == "refresh_ticket_for_segment"
                and operation.segment_id
                and operation.segment_id not in seen
            ):
                seen.add(operation.segment_id)
                segment_ids.append(operation.segment_id)
        return segment_ids

    def _requires_route_refresh(self, operations: list[ItineraryPatchOperation]) -> bool:
        route_affecting_ops = {
            "replace_itinerary",
            "add_segment",
            "replace_segment_poi",
            "replace_segment_poi_from_candidate",
            "remove_segment",
            "move_segment",
            "reorder_segments",
            "replace_transport_mode",
            "refresh_routes_for_day",
        }
        return any(operation.op in route_affecting_ops for operation in operations)

    def _requires_schedule_recompute(self, operations: list[ItineraryPatchOperation]) -> bool:
        schedule_affecting_ops = {
            "replace_itinerary",
            "add_segment",
            "replace_segment_start_time",
            "replace_segment_duration",
            "replace_segment_poi",
            "replace_segment_poi_from_candidate",
            "replace_transport_mode",
            "refresh_routes_for_day",
        }
        # Newly generated/replaced plans and added route anchors carry planning
        # slot times, not user locks. Recompute them after official route refresh
        # so displayed times follow the persisted travel evidence.
        return any(operation.op in schedule_affecting_ops for operation in operations)

    def _preferred_transport_mode(
        self, operations: list[ItineraryPatchOperation], planning_context: Optional[dict] = None
    ) -> Optional[str]:
        if isinstance(planning_context, dict):
            explicit_mode = str(
                planning_context.get("preferredRouteMode") or planning_context.get("preferredMode") or ""
            ).strip()
            if explicit_mode:
                return explicit_mode
            include_modes = planning_context.get("includeModes")
            if isinstance(include_modes, list):
                for mode in include_modes:
                    normalized = str(mode or "").strip()
                    if normalized:
                        return normalized
        for operation in reversed(operations):
            if operation.op == "replace_transport_mode" and operation.value:
                return operation.value.strip()
        return None

    def _route_scope_before(self, plan_id: str, operations: list[ItineraryPatchOperation]) -> Optional[dict]:
        if any(operation.op == "replace_itinerary" for operation in operations):
            return None
        day_ids: set[str] = set()
        target_segment_ids: set[str] = set()
        for operation in operations:
            if (
                operation.op
                in {
                    "replace_segment_poi",
                    "replace_segment_poi_from_candidate",
                    "replace_transport_mode",
                    "remove_segment",
                    "move_segment",
                }
                and operation.segment_id
            ):
                segment = self._segment(plan_id, operation.segment_id)
                if segment is not None:
                    day_ids.add(segment["day_id"])
                    target_segment_ids.add(segment["id"])
            if operation.op == "add_segment" and operation.day_id:
                day_ids.add(operation.day_id)
            if operation.op == "reorder_segments" and operation.day_id:
                day_ids.add(operation.day_id)
            if operation.op == "refresh_routes_for_day" and operation.day_id:
                day_ids.add(operation.day_id)
                for segment in self._segments_for_day(plan_id, operation.day_id):
                    target_segment_ids.add(segment["id"])
            if operation.op == "move_segment":
                target_day_id = operation.target_day_id or operation.day_id
                if target_day_id and target_day_id != NEW_DAY_TARGET_ID:
                    day_ids.add(target_day_id)
        before_pairs = self._adjacent_pairs_for_days(plan_id, day_ids)
        return {
            "day_ids": day_ids,
            "target_segment_ids": target_segment_ids,
            "before_pairs": before_pairs,
            "target_pairs": self._pairs_touching(before_pairs, target_segment_ids),
        }

    def _route_pairs_after(self, plan_id: str, scope: Optional[dict]) -> Optional[set[tuple[str, str]]]:
        if scope is None:
            return None
        day_ids = scope["day_ids"]
        target_segment_ids = scope["target_segment_ids"]
        before_pairs = scope["before_pairs"]
        after_pairs = self._adjacent_pairs_for_days(plan_id, day_ids)
        changed_pairs = before_pairs.symmetric_difference(after_pairs)
        target_pairs = scope["target_pairs"].union(self._pairs_touching(after_pairs, target_segment_ids))
        return changed_pairs.union(target_pairs)

    def _portfolio_meal_route_pairs(
        self,
        plan_id: str,
        route_pairs: Optional[set[tuple[str, str]]],
    ) -> set[tuple[str, str]]:
        """Return only route-anchor pairs touching a persisted meal segment."""
        if not route_pairs:
            return set()
        meal_pairs: set[tuple[str, str]] = set()
        for from_segment_id, to_segment_id in route_pairs:
            row = self.db.execute(
                """
                SELECT from_segment.kind AS from_kind, to_segment.kind AS to_kind
                FROM itinerary_segments from_segment
                JOIN itinerary_segments to_segment
                  ON to_segment.id = ? AND to_segment.plan_id = ?
                WHERE from_segment.id = ? AND from_segment.plan_id = ?
                """,
                (to_segment_id, plan_id, from_segment_id, plan_id),
            ).fetchone()
            if row is not None and (str(row["from_kind"] or "") == "meal" or str(row["to_kind"] or "") == "meal"):
                meal_pairs.add((from_segment_id, to_segment_id))
        return meal_pairs

    def _adjacent_pairs_for_days(self, plan_id: str, day_ids: set[str]) -> set[tuple[str, str]]:
        pairs: set[tuple[str, str]] = set()
        for day_id in day_ids:
            rows = self._route_anchor_rows_for_day(plan_id, day_id)
            for from_segment, to_segment in zip(rows, rows[1:]):
                pairs.add((from_segment["id"], to_segment["id"]))
        return pairs

    def _route_anchor_rows_for_day(self, plan_id: str, day_id: str) -> list[sqlite3.Row]:
        rows = self.db.execute(
            """
            SELECT
                s.*,
                p.amap_id, p.latitude, p.longitude, p.source, p.confidence,
                p.name AS poi_name, p.city AS poi_city, p.category AS poi_category, p.source_note
            FROM itinerary_segments s
            JOIN pois p ON p.id = s.poi_id
            WHERE s.plan_id = ? AND s.day_id = ?
            ORDER BY s.segment_order ASC, s.start_time ASC
            """,
            (plan_id, day_id),
        ).fetchall()
        return [row for row in rows if self._is_route_anchor_row(row)]

    def _is_route_anchor_row(self, row: sqlite3.Row) -> bool:
        kind = str(row["kind"] or "")
        if kind == "park":
            return ItineraryService(self.db)._is_grounded_park_route_anchor_from_row(
                row, name_key="poi_name", category_key="poi_category"
            )
        metadata = self._poi_grounding_metadata(row)
        unresolved_statuses = {
            "optional_waiting",
            "not_required",
            "draft_only",
            "waiting_for_poi_grounding",
            "area_unresolved",
            "provider_rate_limited",
            "area_poi",
            "functional_poi",
            "composite_poi",
        }
        if kind in {"visit", "activity"}:
            return not metadata.get("needsConcretePoi") and metadata.get("groundingStatus") not in unresolved_statuses
        if kind != "meal":
            return False
        return bool(metadata.get("routeable")) and metadata.get("groundingStatus") not in unresolved_statuses

    def _pairs_touching(self, pairs: set[tuple[str, str]], segment_ids: set[str]) -> set[tuple[str, str]]:
        if not segment_ids:
            return set()
        return {pair for pair in pairs if pair[0] in segment_ids or pair[1] in segment_ids}

    def _pending_candidate_id(self, planning_context: Optional[dict]) -> Optional[str]:
        if not isinstance(planning_context, dict):
            return None
        candidate_id = planning_context.get("pendingPoiCandidateId")
        return candidate_id.strip() if isinstance(candidate_id, str) and candidate_id.strip() else None

    def _pending_candidate_ids(
        self, planning_context: Optional[dict], operations: list[ItineraryPatchOperation]
    ) -> list[str]:
        ids: list[str] = []
        context_id = self._pending_candidate_id(planning_context)
        if context_id:
            ids.append(context_id)
        ids.extend(
            operation.candidate_id.strip()
            for operation in operations
            if operation.candidate_id and operation.candidate_id.strip()
        )
        deduped: list[str] = []
        for candidate_id in ids:
            if candidate_id not in deduped:
                deduped.append(candidate_id)
        return deduped

    def _selected_amap_id(self, operations: list[ItineraryPatchOperation]) -> Optional[str]:
        for operation in operations:
            if operation.amap_poi is not None and operation.amap_poi.id:
                return operation.amap_poi.id
        return None

    def _validate_pending_candidate(
        self, session_id: str, candidate_id: str, operations: list[ItineraryPatchOperation]
    ) -> list[str]:
        errors = []
        candidate = self.db.execute(
            "SELECT status, candidates_json, segment_id, selected_amap_id FROM amap_poi_candidates WHERE id = ? AND session_id = ?",
            (candidate_id, session_id),
        ).fetchone()
        if candidate is None:
            return [f"Pending POI candidate not found for this session: {candidate_id}"]
        selected_amap_id = self._selected_amap_id(operations)
        if not selected_amap_id:
            errors.append("Pending POI confirmation requires selected AMap POI")
            return errors
        candidate_status = str(candidate["status"] or "")
        prior_selected_amap_id = str(candidate["selected_amap_id"] or "")
        if candidate_status == "selected":
            if prior_selected_amap_id == selected_amap_id:
                raise HTTPException(
                    status_code=409,
                    detail="Pending POI candidate selection was already applied and is not an exact replay",
                )
            else:
                raise HTTPException(
                    status_code=409, detail="Pending POI candidate was already selected with a different AMap POI"
                )
        elif candidate_status == "selecting":
            raise HTTPException(status_code=409, detail="Pending POI candidate is already being selected")
        elif candidate_status != "pending":
            errors.append(f"Pending POI candidate is not pending: {candidate_id}")
        try:
            candidates = json.loads(candidate["candidates_json"] or "[]")
        except json.JSONDecodeError:
            candidates = []
        candidate_ids = {
            str(item.get("id") or item.get("amapId") or item.get("amap_id") or "").strip()
            for item in candidates
            if isinstance(item, dict)
        }
        if selected_amap_id not in candidate_ids:
            errors.append("Selected AMap POI is not part of the pending candidate set")
        source_segment_id = candidate["segment_id"] if "segment_id" in candidate.keys() else None
        if source_segment_id:
            matching_ops = [operation for operation in operations if operation.candidate_id == candidate_id]
            if not matching_ops:
                errors.append("Pending POI candidate requires an explicit replace_segment_poi_from_candidate operation")
            elif any(operation.segment_id != source_segment_id for operation in matching_ops):
                errors.append("Pending POI candidate does not belong to the target segment")
            selected_operation = next(
                (
                    operation
                    for operation in matching_ops
                    if operation.amap_poi is not None and operation.amap_poi.id == selected_amap_id
                ),
                None,
            )
            intent_type = self._segment_intent_type(str(source_segment_id))
            if selected_operation is not None and intent_type:
                semantic = self.intent_candidate_semantic_policy.evaluate(intent_type, selected_operation.amap_poi)
                if not semantic.passed:
                    errors.append(
                        f"Selected AMap POI does not satisfy segment intent {intent_type}: {semantic.reason_code}"
                    )
        return errors

    def _selected_pending_candidate_replay_response(
        self,
        session: sqlite3.Row,
        plan_id: str,
        operations: list[ItineraryPatchOperation],
        candidate_id: Optional[str],
        *,
        base_version_id: Optional[str],
    ) -> Optional[ItineraryPatchResponse]:
        """Return the prior accepted result for one exact candidate-selection replay.

        A consumed candidate is an opaque, one-shot command.  Retrying the
        identical command against the current active version must therefore be
        a read-only response, not a second patch/version/route refresh.  Any
        drift in the target segment, candidate identity, or stored lineage is
        a conflict rather than permission to resurrect the consumed choice.
        """

        if not candidate_id or len(operations) != 1:
            return None
        operation = operations[0]
        if (
            operation.op not in {"replace_segment_poi_from_candidate", "add_segment"}
            or (operation.candidate_id and operation.candidate_id != candidate_id)
            or operation.amap_poi is None
        ):
            return None

        candidate = self.db.execute(
            """
            SELECT status, candidates_json, segment_id, selected_amap_id
            FROM amap_poi_candidates
            WHERE id = ? AND session_id = ?
            """,
            (candidate_id, session["id"]),
        ).fetchone()
        if candidate is None or str(candidate["status"] or "") != "selected":
            return None

        selected_amap_id = str(candidate["selected_amap_id"] or "")
        requested_amap_id = str(operation.amap_poi.id or "")
        if selected_amap_id != requested_amap_id:
            raise HTTPException(
                status_code=409,
                detail="Pending POI candidate was already selected with a different AMap POI",
            )
        source_segment_id = str(candidate["segment_id"] or "")
        if source_segment_id and (
            operation.op != "replace_segment_poi_from_candidate" or source_segment_id != str(operation.segment_id or "")
        ):
            raise HTTPException(
                status_code=409,
                detail="Pending POI candidate replay does not belong to the target segment",
            )
        if not source_segment_id and operation.op != "add_segment":
            raise HTTPException(
                status_code=409,
                detail="Pending POI candidate replay operation does not match its persisted scope",
            )

        canonical = self._selected_candidate_payload(candidate["candidates_json"], selected_amap_id)
        if canonical is None:
            raise HTTPException(
                status_code=409,
                detail="Selected AMap POI is no longer present in the persisted candidate set",
            )
        self._assert_public_amap_identity_matches(operation.amap_poi, canonical)

        active_version_id = str(session["active_version_id"] or "")
        lineage = self._accepted_candidate_selection_lineage(
            str(session["id"]),
            plan_id,
            candidate_id,
            selected_amap_id,
            active_version_id,
            operation,
        )
        if lineage is None:
            raise HTTPException(
                status_code=409,
                detail="Pending POI candidate selection lineage is missing",
            )
        lineage_row, persisted_operation = lineage
        target_segment_id = source_segment_id or str(persisted_operation.get("segmentId") or "")
        segment = self._segment_with_poi(plan_id, target_segment_id)
        if segment is None or not self._segment_matches_amap_identity(segment, canonical):
            raise HTTPException(
                status_code=409,
                detail="Pending POI candidate was already selected but the target segment has changed",
            )
        accepted_base_ids = {str(lineage_row["base_version_id"] or ""), active_version_id}
        if not base_version_id or str(base_version_id) not in accepted_base_ids:
            return None

        return ItineraryPatchResponse(
            itinerary=ItineraryService(self.db).get_plan(plan_id),
            patch=ItineraryPatchSummaryResponse(
                id=str(lineage_row["patch_id"]),
                validation_status="accepted",
                metadata={
                    "idempotentReplay": True,
                    "candidateId": candidate_id,
                    "selectedAmapId": selected_amap_id,
                    "versionDelta": 0,
                    "patchDelta": 0,
                    "routeWriteDelta": 0,
                },
            ),
            version=ItineraryVersionResponse(
                id=str(lineage_row["version_id"]),
                version_number=int(lineage_row["version_number"]),
                source_type=str(lineage_row["version_source_type"]),
            ),
            validation_errors=[],
            pending_poi_candidates=self._pending_candidates_payload(str(session["id"])),
        )

    @staticmethod
    def _selected_candidate_payload(raw_candidates: object, selected_amap_id: str) -> Optional[MapPoiResponse]:
        try:
            candidates = json.loads(str(raw_candidates or "[]"))
        except json.JSONDecodeError:
            return None
        for payload in candidates if isinstance(candidates, list) else []:
            if not isinstance(payload, dict):
                continue
            payload_id = str(payload.get("id") or payload.get("amapId") or payload.get("amap_id") or "")
            if payload_id != selected_amap_id:
                continue
            try:
                return MapPoiResponse.model_validate(payload)
            except (TypeError, ValueError):
                return None
        return None

    @staticmethod
    def _segment_matches_amap_identity(segment: sqlite3.Row, canonical: MapPoiResponse) -> bool:
        if (
            str(segment["source"] or "") != AMAP_PLACE_SOURCE
            or str(segment["amap_id"] or "") != str(canonical.id or "")
            or str(segment["poi_name"] or "") != str(canonical.name or "")
        ):
            return False
        try:
            return (
                abs(float(segment["longitude"]) - float(canonical.longitude)) <= 1e-6
                and abs(float(segment["latitude"]) - float(canonical.latitude)) <= 1e-6
            )
        except (TypeError, ValueError):
            return False

    def _accepted_candidate_selection_lineage(
        self,
        session_id: str,
        plan_id: str,
        candidate_id: str,
        selected_amap_id: str,
        active_version_id: str,
        replay_operation: ItineraryPatchOperation,
    ) -> Optional[tuple[sqlite3.Row, dict[str, Any]]]:
        rows = self.db.execute(
            """
            SELECT
                p.id AS patch_id,
                p.base_version_id,
                p.operations_json,
                v.id AS version_id,
                v.version_number,
                v.source_type AS version_source_type
            FROM itinerary_patches p
            JOIN itinerary_versions v
              ON v.id = p.result_version_id
             AND v.source_patch_id = p.id
             AND v.session_id = p.session_id
             AND v.plan_id = p.plan_id
            WHERE p.session_id = ? AND p.plan_id = ?
              AND p.validation_status = 'accepted'
              AND p.result_version_id = ?
            ORDER BY p.created_at DESC, p.id DESC
            """,
            (session_id, plan_id, active_version_id),
        ).fetchall()
        for row in rows:
            try:
                payload = json.loads(row["operations_json"] or "[]")
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
                continue
            operation = payload[0]
            amap_poi = operation.get("amapPoi") if isinstance(operation.get("amapPoi"), dict) else {}
            if (
                str(operation.get("candidateId") or "") == candidate_id
                and str(amap_poi.get("id") or "") == selected_amap_id
                and self._candidate_replay_operation_matches(operation, replay_operation, candidate_id)
            ):
                return row, operation
        return None

    @staticmethod
    def _candidate_replay_operation_matches(
        persisted: dict[str, Any],
        replay: ItineraryPatchOperation,
        candidate_id: str,
    ) -> bool:
        if str(persisted.get("op") or "") != replay.op:
            return False
        if str(persisted.get("candidateId") or "") != candidate_id:
            return False
        replay_payload = replay.model_dump(by_alias=True)
        comparable_fields = {
            "value",
            "dayId",
            "targetDayId",
            "startTime",
            "title",
            "kind",
            "intentType",
            "notes",
            "durationMinutes",
            "estimatedCost",
            "transportMode",
            "allowUnresolved",
            "radius",
            "orderedSegmentIds",
            "semanticMetadata",
        }
        if replay.op != "add_segment":
            comparable_fields.add("segmentId")
        return all(persisted.get(field) == replay_payload.get(field) for field in comparable_fields)

    @staticmethod
    def _bind_pending_candidate_id(
        operations: list[ItineraryPatchOperation], candidate_id: str
    ) -> list[ItineraryPatchOperation]:
        selectable = [
            index
            for index, operation in enumerate(operations)
            if operation.amap_poi is not None and not operation.candidate_id
        ]
        if len(selectable) != 1:
            return operations
        selected_index = selectable[0]
        return [
            operation.model_copy(update={"candidate_id": candidate_id}) if index == selected_index else operation
            for index, operation in enumerate(operations)
        ]

    def _segment_intent_type(self, segment_id: str) -> str:
        row = self.db.execute(
            """
            SELECT s.notes, p.source_note
            FROM itinerary_segments s
            LEFT JOIN pois p ON p.id = s.poi_id
            WHERE s.id = ?
            """,
            (segment_id,),
        ).fetchone()
        if row is None:
            return ""
        text = f"{row['notes'] or ''} {row['source_note'] or ''}"
        match = re.search(r"intentType\s*[：:=]\s*([a-z_]+)", text, flags=re.IGNORECASE)
        return match.group(1).lower() if match else ""

    def _user_confirmed_segment_protection_errors(
        self, plan_id: str, operations: list[ItineraryPatchOperation]
    ) -> list[str]:
        errors: list[str] = []
        protected_ops = {"replace_segment_poi", "replace_segment_poi_from_candidate", "remove_segment"}
        for operation in operations:
            if operation.op not in protected_ops or not operation.segment_id:
                continue
            row = self._segment_with_poi(plan_id, operation.segment_id)
            if row is None:
                continue
            metadata = self._poi_grounding_metadata(row)
            if metadata.get("groundingStatus") == "user_confirmed":
                errors.append(f"Agent cannot overwrite user_confirmed segment: {operation.segment_id}")
        return errors

    def _claim_pending_candidate_selection(self, session_id: str, candidate_id: str, selected_amap_id: str) -> bool:
        candidate = self.db.execute(
            "SELECT status, selected_amap_id FROM amap_poi_candidates WHERE id = ? AND session_id = ?",
            (candidate_id, session_id),
        ).fetchone()
        if candidate is None:
            raise HTTPException(status_code=409, detail="Pending POI candidate disappeared before patch commit")
        status = str(candidate["status"] or "")
        existing_selected_amap_id = str(candidate["selected_amap_id"] or "")
        if status == "selected" and existing_selected_amap_id == selected_amap_id:
            return False
        if status == "selected":
            raise HTTPException(
                status_code=409, detail="Pending POI candidate was already selected with a different AMap POI"
            )
        if status == "selecting":
            raise HTTPException(status_code=409, detail="Pending POI candidate is already being selected")
        if status != "pending":
            raise HTTPException(
                status_code=409, detail=f"Pending POI candidate is no longer selectable: {candidate_id}"
            )
        cursor = self.db.execute(
            """
            UPDATE amap_poi_candidates
            SET status = 'selecting', selected_amap_id = ?
            WHERE id = ? AND session_id = ? AND status = 'pending'
            """,
            (selected_amap_id, candidate_id, session_id),
        )

        if cursor.rowcount != 1:
            raise HTTPException(status_code=409, detail="Pending POI candidate state changed before patch commit")
        return True

    def _rollback_pending_candidate_claim(self, session_id: str, candidate_id: str, selected_amap_id: str) -> None:
        if not candidate_id:
            return
        self.db.execute(
            """
            UPDATE amap_poi_candidates
            SET status = 'pending', selected_amap_id = NULL
            WHERE id = ? AND session_id = ? AND status = 'selecting' AND selected_amap_id = ?
            """,
            (candidate_id, session_id, selected_amap_id),
        )

    def _normalize_visit_durations(
        self,
        operations: list[ItineraryPatchOperation],
        planning_context: Optional[dict],
    ) -> list[ItineraryPatchOperation]:
        normalized: list[ItineraryPatchOperation] = []
        policy = VisitDurationPolicy()
        preserve_saved_simple_direction_schedule = bool(
            isinstance(planning_context, dict)
            and planning_context.get("_serverPreserveSavedSimpleDirectionSchedule") is True
        )
        for operation in operations:
            if operation.op != "replace_itinerary" or not isinstance(operation.full_itinerary, dict):
                normalized.append(operation)
                continue
            if preserve_saved_simple_direction_schedule:
                # This material was previously reloaded from an active version
                # and saved back into the server-owned Simple Direction store.
                # Its complete time/estimate envelope is authoritative user
                # work.  Re-running VisitDurationPolicy here would erase
                # userLocked duration metadata and silently move downstream
                # segments while restoring A after editing B.
                normalized.append(operation)
                continue
            payload = operation.model_dump(by_alias=True)
            snapshot = payload.get("fullItinerary")
            if isinstance(snapshot, dict):
                for day in snapshot.get("days") or []:
                    if not isinstance(day, dict):
                        continue
                    for segment in day.get("segments") or []:
                        if isinstance(segment, dict):
                            policy.normalize_segment_dict(segment, planning_context or {})
            normalized.append(ItineraryPatchOperation.model_validate(payload))
        return normalized

    def _mark_pending_candidate_selected(self, session_id: str, candidate_id: str, selected_amap_id: str) -> None:
        existing = self.db.execute(
            "SELECT status, selected_amap_id FROM amap_poi_candidates WHERE id = ? AND session_id = ?",
            (candidate_id, session_id),
        ).fetchone()
        if (
            existing is not None
            and existing["status"] == "selected"
            and existing["selected_amap_id"] == selected_amap_id
        ):
            return
        cursor = self.db.execute(
            """
            UPDATE amap_poi_candidates
            SET status = 'selected', selected_amap_id = ?
            WHERE id = ? AND session_id = ? AND status IN ('pending', 'selecting')
            """,
            (selected_amap_id, candidate_id, session_id),
        )
        if cursor.rowcount != 1:
            raise HTTPException(status_code=409, detail="Pending POI candidate state changed before patch commit")

    def _insert_patch(
        self,
        session_id: str,
        plan_id: str,
        base_version_id: Optional[str],
        result_version_id: Optional[str],
        source_type: str,
        source_turn_id: Optional[str],
        operations: list[ItineraryPatchOperation],
        validation_status: str,
        validation_errors: list[str],
        mutation_id: Optional[str] = None,
    ) -> str:
        patch_id = f"patch_{uuid4().hex[:12]}"
        self.db.execute(
            """
            INSERT INTO itinerary_patches (
                id, session_id, plan_id, base_version_id, result_version_id,
                source_type, source_turn_id, planning_run_id, mutation_id, operations_json, validation_status,
                validation_errors_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                patch_id,
                session_id,
                plan_id,
                base_version_id,
                result_version_id,
                source_type,
                source_turn_id,
                None,
                mutation_id,
                json.dumps([operation.model_dump(by_alias=True) for operation in operations], ensure_ascii=False),
                validation_status,
                json.dumps(validation_errors, ensure_ascii=False),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        return patch_id

    def _day_exists(self, plan_id: str, day_id: str) -> bool:
        return (
            self.db.execute(
                "SELECT 1 FROM itinerary_days WHERE id = ? AND plan_id = ?",
                (day_id, plan_id),
            ).fetchone()
            is not None
        )

    def _segment(self, plan_id: str, segment_id: str) -> Optional[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM itinerary_segments WHERE id = ? AND plan_id = ?",
            (segment_id, plan_id),
        ).fetchone()

    def _segment_with_poi(self, plan_id: str, segment_id: str) -> Optional[sqlite3.Row]:
        return self.db.execute(
            """
            SELECT
                s.*,
                p.amap_id, p.latitude, p.longitude, p.source, p.confidence,
                p.name AS poi_name, p.city AS poi_city, p.category AS poi_category, p.source_note
            FROM itinerary_segments s
            JOIN pois p ON p.id = s.poi_id
            WHERE s.id = ? AND s.plan_id = ?
            """,
            (segment_id, plan_id),
        ).fetchone()

    def _segments_for_day(self, plan_id: str, day_id: str) -> list[sqlite3.Row]:
        return self.db.execute(
            """
            SELECT * FROM itinerary_segments
            WHERE plan_id = ? AND day_id = ?
            ORDER BY segment_order ASC, start_time ASC
            """,
            (plan_id, day_id),
        ).fetchall()

    def _validate_reorder_segments(self, plan_id: str, day_id: str, ordered_segment_ids: list[str]) -> list[str]:
        current_segments = self._segments_for_day(plan_id, day_id)
        current_ids = [row["id"] for row in current_segments]
        errors = []
        if len(ordered_segment_ids) != len(set(ordered_segment_ids)):
            errors.append("orderedSegmentIds must not contain duplicates")
            return errors
        if set(ordered_segment_ids) != set(current_ids):
            plan_segments = {
                row["id"]: row["day_id"]
                for row in self.db.execute(
                    "SELECT id, day_id FROM itinerary_segments WHERE plan_id = ?", (plan_id,)
                ).fetchall()
            }
            if any(
                segment_id in plan_segments and plan_segments[segment_id] != day_id
                for segment_id in ordered_segment_ids
            ):
                errors.append("orderedSegmentIds cannot include segments from another day")
            else:
                errors.append("orderedSegmentIds must include every segment in the day exactly once")
        return errors

    def _validate_replacement_snapshot(
        self,
        plan_id: str,
        snapshot: Optional[dict],
        *,
        allow_soft_pending_days: bool = False,
        allow_server_sealed_zero_target_days: bool = False,
    ) -> list[str]:
        if not snapshot:
            return ["replace_itinerary requires fullItinerary"]
        try:
            normalized = self._normalize_replacement_snapshot(plan_id, snapshot)
        except (KeyError, TypeError, ValueError) as error:
            return [f"Invalid fullItinerary: {error}"]
        errors = []
        if not normalized.get("title", "").strip():
            errors.append("Itinerary title cannot be empty")
        if not normalized.get("days"):
            errors.append("replace_itinerary requires at least one day")
        partial_timeline = isinstance(snapshot.get("portfolioPartialTimeline"), dict)
        pending_days = {
            int(item.get("dayNumber") or 0)
            for item in snapshot.get("portfolioPendingSlots") or []
            if isinstance(item, dict)
        }
        soft_pending_days = {
            int(item.get("dayNumber") or 0)
            for item in snapshot.get("portfolioPendingSlots") or []
            if isinstance(item, dict) and str(item.get("requirementLevel") or "").strip() in {"soft", "explicit_soft"}
        }
        hard_pending_days = {
            int(item.get("dayNumber") or 0)
            for item in snapshot.get("portfolioPendingSlots") or []
            if isinstance(item, dict) and str(item.get("requirementLevel") or "").strip() in {"required", "hard"}
        }
        zero_target_days = self._server_sealed_simple_open_zero_target_days(
            snapshot,
            normalized,
            enabled=allow_server_sealed_zero_target_days,
        )
        for day in normalized.get("days", []):
            day_number = int(day.get("dayNumber") or 0)
            if (
                not day.get("segments")
                and not (partial_timeline and day_number in pending_days)
                and not (
                    allow_soft_pending_days and day_number in soft_pending_days and day_number not in hard_pending_days
                )
                and day_number not in zero_target_days
            ):
                errors.append(f"Day {day.get('dayNumber')} requires at least one segment")
            seen_intervals = []
            for segment in day.get("segments", []):
                start_time = segment.get("startTime", "")
                end_time = segment.get("endTime", "")
                if not self._valid_clock(start_time) or not self._valid_clock(end_time):
                    errors.append("Segment time must use HH:mm format")
                    continue
                start = self._minutes(start_time)
                end = self._minutes(end_time)
                if end <= start or end > 24 * 60:
                    errors.append("Segment end time cannot cross days")
                    continue
                if any(
                    start < existing_end and end > existing_start for existing_start, existing_end in seen_intervals
                ):
                    errors.append("Segment time conflicts with another segment")
                seen_intervals.append((start, end))
                poi_error = self._validate_snapshot_poi(segment.get("poi"))
                if poi_error:
                    errors.append(poi_error)
        return errors

    @staticmethod
    def _server_sealed_simple_open_zero_target_days(
        snapshot: dict,
        normalized: dict,
        *,
        enabled: bool,
    ) -> set[int]:
        """Return only explicitly sealed zero-density calendar days.

        The caller mints ``enabled`` from the server-only proposal commit path.
        The snapshot contract is still checked in full here so malformed,
        partial, all-zero, or positive-target calendars remain fail-closed.
        """

        if not enabled or snapshot.get("simpleOpenExecutionProfile") != "simple_open_v1":
            return set()
        raw_targets = snapshot.get("desiredDensityAnchorTargets")
        if not isinstance(raw_targets, dict) or not raw_targets:
            return set()
        day_numbers: list[int] = []
        for day in normalized.get("days") or []:
            day_number = day.get("dayNumber")
            if isinstance(day_number, bool) or not isinstance(day_number, int) or day_number <= 0:
                return set()
            day_numbers.append(day_number)
        if not day_numbers or len(day_numbers) != len(set(day_numbers)):
            return set()
        expected_keys = {str(day_number) for day_number in day_numbers}
        if set(raw_targets) != expected_keys or any(not isinstance(key, str) for key in raw_targets):
            return set()
        targets: dict[int, int] = {}
        for key, value in raw_targets.items():
            if (
                not key.isdigit()
                or str(int(key)) != key
                or isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
            ):
                return set()
            targets[int(key)] = value
        if not any(value > 0 for value in targets.values()):
            return set()
        zero_target_days = {day_number for day_number, value in targets.items() if value == 0}
        positive_target_days = {day_number for day_number, value in targets.items() if value > 0}

        def normalized_day_contract(value: object) -> Optional[set[int]]:
            if not isinstance(value, list):
                return None
            normalized: set[int] = set()
            for raw_day in value:
                if isinstance(raw_day, bool) or not isinstance(raw_day, int) or raw_day <= 0:
                    return None
                normalized.add(raw_day)
            if len(normalized) != len(value):
                return None
            return normalized

        required_days = normalized_day_contract(snapshot.get("requiredPlanningDayNumbers"))
        explicit_rest_days = normalized_day_contract(snapshot.get("explicitRestDayNumbers"))
        calendar_days = set(day_numbers)
        if (
            required_days is None
            or explicit_rest_days is None
            or required_days & explicit_rest_days
            or required_days | explicit_rest_days != calendar_days
            or required_days != positive_target_days
            or explicit_rest_days != zero_target_days
        ):
            return set()
        return explicit_rest_days

    def _normalize_replacement_snapshot(self, plan_id: str, snapshot: dict) -> dict:
        plan = self.db.execute("SELECT * FROM itinerary_plans WHERE id = ?", (plan_id,)).fetchone()
        if plan is None:
            raise HTTPException(status_code=404, detail="Itinerary plan not found")
        normalized_days = []
        total_cost = 0.0
        budget_target = self._optional_float(snapshot.get("budgetTarget", plan["budget_target"]))
        budget_hint = snapshot.get("budgetTarget", plan["budget_target"])
        meal_allowance_applied = False
        for day_index, day in enumerate(snapshot.get("days", []), start=1):
            day_id = day.get("id") or f"day_{uuid4().hex[:12]}"
            segments = []
            day_cost = 0.0
            for segment in day.get("segments", []):
                poi = dict(segment["poi"])
                poi.setdefault("id", f"poi_{uuid4().hex[:12]}")
                cost = float(segment.get("estimatedCost", 0) or 0)
                if cost <= 0 and str(segment.get("kind") or "").strip() == "meal":
                    cost = self._meal_allowance_for_snapshot_segment(segment, budget_hint)
                    meal_allowance_applied = cost > 0
                day_cost += cost
                segments.append(
                    {
                        "id": segment.get("id") or f"seg_{uuid4().hex[:12]}",
                        "startTime": segment["startTime"],
                        "endTime": segment["endTime"],
                        "kind": segment.get("kind", "activity"),
                        "poi": poi,
                        "transportMode": segment.get("transportMode", "walk"),
                        "estimatedCost": cost,
                        "estimateMetadata": segment.get("estimateMetadata")
                        if isinstance(segment.get("estimateMetadata"), dict)
                        else {},
                        "semanticMetadata": segment.get("semanticMetadata")
                        if isinstance(segment.get("semanticMetadata"), dict)
                        else {},
                        "notes": segment.get("notes", ""),
                    }
                )
            total_cost += day_cost
            normalized_days.append(
                {
                    "id": day_id,
                    "dayNumber": int(day.get("dayNumber") or day_index),
                    "date": day.get("date"),
                    "title": day.get("title", f"Day {day_index}"),
                    "weatherSummary": day.get("weatherSummary", ""),
                    "riskSummary": day.get("riskSummary", ""),
                    "totalEstimatedCost": day_cost,
                    "segments": segments,
                }
            )
        return {
            "id": plan_id,
            "title": snapshot.get("title", plan["title"]),
            "city": snapshot.get("city", plan["city"]),
            "templateType": snapshot.get("templateType", plan["template_type"]),
            "budgetTarget": budget_target,
            "budgetTier": snapshot.get("budgetTier", plan["budget_tier"]),
            "budgetEstimate": max(float(snapshot.get("budgetEstimate", total_cost) or 0), total_cost),
            "budgetDeltaExplanation": self._budget_delta_explanation_with_meals(
                snapshot.get("budgetDeltaExplanation", "Agent 生成的预算估算，后续可继续细化。"),
                meal_allowance_applied,
            ),
            "decisionRationale": snapshot.get("decisionRationale", "DeepSeek Agent 根据用户对话生成并经服务端校验。"),
            "status": snapshot.get("status", "draft"),
            "days": normalized_days,
            "routeOptions": snapshot.get("routeOptions", []),
            "weatherSignals": snapshot.get("weatherSignals", []),
            "trafficCrowdingSignals": snapshot.get("trafficCrowdingSignals", []),
            "ticketLookupResults": snapshot.get("ticketLookupResults", []),
        }

    def _meal_allowance_for_snapshot_segment(self, segment: dict[str, Any], budget_hint: object) -> float:
        text = " ".join(
            [
                str(segment.get("id") or ""),
                str(segment.get("startTime") or ""),
                str(segment.get("endTime") or ""),
                str(segment.get("notes") or ""),
                str((segment.get("poi") or {}).get("name") or ""),
                str((segment.get("poi") or {}).get("sourceNote") or ""),
            ]
        )
        policy = MealGroundingPolicy()
        label = policy.label_for_text(text)
        if label is None:
            minutes = self._minutes(str(segment.get("startTime") or "12:00"))
            if minutes < 10 * 60:
                label = "breakfast"
            elif minutes >= 17 * 60:
                label = "dinner"
            else:
                label = "lunch"
        return float(policy.estimate_cost(label, budget_hint, text))

    def _budget_delta_explanation_with_meals(self, explanation: object, meal_allowance_applied: bool) -> str:
        text = str(explanation or "Agent 生成的预算估算，后续可继续细化。")
        if not meal_allowance_applied or "餐饮" in text:
            return text
        return f"{text} 普通用餐时间已按预算档加入餐饮 allowance，真实门店价格以用户最终选择为准。"

    def _optional_float(self, value: object) -> Optional[float]:
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value).strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None

    def _validate_snapshot_poi(self, poi: Optional[dict]) -> str:
        if not poi:
            return "Segment POI is required"
        if poi.get("source") == "agent-text-timeline":
            if not poi.get("name"):
                return "Segment POI name cannot be empty"
            if poi.get("longitude") is not None or poi.get("latitude") is not None:
                return "Agent text timeline POI coordinates must stay empty until map grounding"
            if not isinstance(poi.get("confidence"), (int, float)):
                return "Segment POI confidence must be a number"
            return ""
        amap_id = poi.get("amapId") or poi.get("amap_id")
        if not amap_id:
            return "Segment POI requires amapId"
        if poi.get("source") != AMAP_PLACE_SOURCE:
            return "Segment POI must come from AMap"
        if not isinstance(poi.get("confidence"), (int, float)):
            return "Segment POI confidence must be a number"
        if float(poi.get("confidence", 0) or 0) < 0.8:
            return "Segment POI confidence is too low"
        if not poi.get("name"):
            return "Segment POI name cannot be empty"
        if not isinstance(poi.get("longitude"), (int, float)) or not isinstance(poi.get("latitude"), (int, float)):
            return "Segment POI requires real coordinates"
        return ""

    def _validate_resolved_amap_poi(self, poi: Optional[MapPoiResponse], operation_name: str) -> str:
        if poi is None:
            return f"{operation_name} requires resolved AMap POI"
        if not poi.id:
            return f"{operation_name} requires AMap POI id"
        if poi.source != AMAP_PLACE_SOURCE:
            return f"{operation_name} POI must come from AMap"
        if poi.confidence < 0.8:
            return f"{operation_name} POI confidence is too low"
        if not poi.name.strip():
            return f"{operation_name} POI name cannot be empty"
        return ""

    def _meal_operation_uses_non_food(self, plan_id: str, operation: ItineraryPatchOperation) -> bool:
        """An area anchor can locate a restaurant but must never become the meal itself."""
        if operation.amap_poi is None:
            return False
        kind = str(operation.kind or "")
        if operation.segment_id:
            segment = self._segment(plan_id, operation.segment_id)
            kind = str(segment["kind"] or "") if segment is not None else kind
        if kind != "meal":
            return False
        poi = operation.amap_poi
        text = " ".join([str(poi.name or ""), str(poi.type or ""), str(poi.category or "")])
        is_food = bool(re.search(r"(餐饮|餐厅|饭店|中餐|西餐|快餐|小吃|咖啡|茶饮|烘焙|美食)", text))
        is_shopping = bool(re.search(r"(商场|购物中心|购物服务|商业综合体|百货)", text))
        return is_shopping and not is_food

    def _validate_segment_time(self, plan_id: str, segment: sqlite3.Row, next_start: str) -> str:
        return self._validate_interval(
            plan_id,
            segment["day_id"],
            next_start,
            self._segment_duration(segment),
            ignore_segment_id=segment["id"],
        )

    def _validate_interval(
        self,
        plan_id: str,
        day_id: str,
        start_time: str,
        duration_minutes: int,
        ignore_segment_id: Optional[str] = None,
    ) -> str:
        if duration_minutes <= 0:
            return "Segment duration must be positive"
        start = self._minutes(start_time)
        end = start + duration_minutes
        if end <= start or end > 24 * 60:
            return "Segment end time cannot cross days"
        rows = self.db.execute(
            """
            SELECT * FROM itinerary_segments
            WHERE plan_id = ? AND day_id = ?
            ORDER BY start_time ASC, segment_order ASC
            """,
            (plan_id, day_id),
        ).fetchall()
        for row in rows:
            if ignore_segment_id and row["id"] == ignore_segment_id:
                continue
            existing_start = self._minutes(row["start_time"])
            existing_end = self._minutes(row["end_time"])
            if start < existing_end and end > existing_start:
                return "Segment time conflicts with another segment"
        return ""

    def _add_day(self, plan_id: str, title: str) -> str:
        row = self.db.execute(
            "SELECT COALESCE(MAX(day_number), 0) AS max_day FROM itinerary_days WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
        day_number = int(row["max_day"]) + 1
        day_id = f"day_{uuid4().hex[:12]}"
        self.db.execute(
            """
            INSERT INTO itinerary_days (
                id, plan_id, day_number, date, title, weather_summary,
                risk_summary, total_estimated_cost
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                day_id,
                plan_id,
                day_number,
                None,
                title.strip(),
                "",
                "",
                0,
            ),
        )
        return day_id

    def _add_segment(self, plan_id: str, operation: ItineraryPatchOperation, *, source_type: str = "manual") -> None:
        day_id = operation.day_id or ""
        start_time = operation.start_time or operation.value or self._next_segment_start_time(plan_id, day_id)
        duration = operation.duration_minutes or 30
        end_time = self._format_minutes(self._minutes(start_time) + duration)
        kind = self._add_segment_kind(operation)
        poi_id = (
            self._insert_resolved_amap_poi(plan_id, operation.amap_poi, source_type=source_type)
            if operation.amap_poi is not None
            else self._insert_skeleton_poi(
                plan_id,
                operation.title or "待定景点/活动",
                category=kind,
                source_note=self._unresolved_segment_source_note(kind),
            )
        )
        segment_id = operation.segment_id or f"seg_{uuid4().hex[:12]}"
        order = self._next_segment_order(plan_id, day_id)
        notes = operation.notes or (
            operation.amap_poi.source_note
            if operation.amap_poi is not None
            else "等待 Agent 补全景点、预约、天气和风险信息。"
        )
        semantic_metadata = self._new_segment_semantic_metadata(operation, kind, notes)
        self.db.execute(
            """
            INSERT INTO itinerary_segments (
                id, plan_id, day_id, segment_order, kind, start_time, end_time,
                poi_id, transport_mode, estimated_cost, semantic_metadata_json, notes, weather_signal_id,
                traffic_crowding_signal_id, ticket_lookup_result_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                segment_id,
                plan_id,
                day_id,
                order,
                kind,
                start_time,
                end_time,
                poi_id,
                operation.transport_mode or "walk",
                float(operation.estimated_cost or 0),
                json.dumps(semantic_metadata, ensure_ascii=False),
                notes,
                None,
                None,
                None,
            ),
        )
        self._reorder_day_segments(plan_id, day_id)

    @staticmethod
    def _new_segment_semantic_metadata(operation: ItineraryPatchOperation, kind: str, notes: str) -> dict[str, Any]:
        poi = operation.amap_poi
        text = f"{operation.title or ''} {getattr(poi, 'name', '')} {getattr(poi, 'type', '')} {getattr(poi, 'category', '')} {notes}"
        intent_type = str(operation.intent_type or "").strip() or next(
            (
                intent
                for intent, pattern in (
                    ("museum", r"美术馆|艺术博物馆|博物馆|博物院|画院|艺术馆"),
                    ("campus_visit", r"大学|学院|高校|校园|校区"),
                    ("meal", r"餐厅|饭店|餐饮|午餐|晚餐|早餐|美食"),
                    ("night_view", r"夜景|夜游|观景台|天际线"),
                    ("park", r"公园|园林|植物园|湿地"),
                )
                if re.search(pattern, text)
            ),
            "meal" if kind == "meal" else None,
        )
        trusted_amap = bool(
            poi
            and poi.source == AMAP_PLACE_SOURCE
            and poi.id
            and poi.longitude is not None
            and poi.latitude is not None
        )
        name = str(getattr(poi, "name", "") or operation.title or "")
        return {
            "intentType": intent_type,
            "intentSlotId": None,
            "rawNeed": notes or name,
            "groundingStatus": "verified_amap" if trusted_amap else "waiting_for_poi_grounding",
            "routeAnchor": trusted_amap,
            "required": bool(intent_type),
            "userLocked": False,
            "aliases": [name] if name else [],
            **(operation.semantic_metadata or {}),
        }

    def _allows_unresolved_add_segment(self, operation: ItineraryPatchOperation) -> bool:
        if not operation.allow_unresolved:
            return False
        return self._add_segment_kind(operation) in {"meal", "rest", "note", "buffer"}

    def _add_segment_kind(self, operation: ItineraryPatchOperation) -> str:
        kind = str(operation.kind or "").strip()
        if kind in {"meal", "rest", "note", "buffer", "visit", "activity", "area_walk"}:
            return kind
        return "activity"

    def _unresolved_segment_source_note(self, kind: str) -> str:
        if kind == "meal":
            return "groundingStatus：waiting_for_poi_grounding；intentType=meal；高德 POI 待校验；可展开附近餐饮候选。"
        return "groundingStatus：waiting_for_poi_grounding；高德 POI 待校验。"

    def _replace_segment_poi(
        self,
        plan_id: str,
        operation: ItineraryPatchOperation,
        *,
        source_type: str = "manual",
        planning_context: Optional[dict] = None,
    ) -> None:
        segment = self._segment(plan_id, operation.segment_id or "")
        if segment is None:
            raise HTTPException(status_code=404, detail="Segment not found")
        poi_id = self._insert_resolved_amap_poi(plan_id, operation.amap_poi, source_type=source_type)
        metadata = self._estimate_metadata(segment)
        duration_metadata = metadata.get("duration") if isinstance(metadata.get("duration"), dict) else {}
        user_locked = bool(duration_metadata.get("userLocked"))
        duration = self._segment_duration(segment)
        duration_override = 0
        if isinstance(planning_context, dict):
            overrides = planning_context.get("_serverExplicitDurationOverrides")
            if isinstance(overrides, dict):
                try:
                    duration_override = int(overrides.get(str(operation.segment_id or "")) or 0)
                except (TypeError, ValueError):
                    duration_override = 0
        if duration_override > 0:
            duration = duration_override
            duration_metadata.update({"userLocked": True, "source": "user_locked", "minutes": duration_override})
            metadata["duration"] = duration_metadata
            user_locked = True
        estimated_cost = float(segment["estimated_cost"] or 0)
        if not user_locked:
            decision = VisitDurationPolicy().normalize_duration(
                None,
                kind=str(segment["kind"] or "visit"),
                category=str(operation.amap_poi.category or operation.amap_poi.type or ""),
                intent_type=str(operation.amap_poi.type or ""),
                context=planning_context or {},
            )
            duration = decision.preferred_minutes
            metadata = {**metadata, **decision.to_metadata()}
            if str(segment["kind"] or "") == "meal":
                tier = self._plan_budget_tier(plan_id)
                label = (
                    MealGroundingPolicy().label_for_text(
                        f"{operation.amap_poi.name} {operation.amap_poi.type} {operation.amap_poi.category}"
                    )
                    or "lunch"
                )
                cost = MealGroundingPolicy().estimate_cost_range(label, tier)
                metadata["cost"] = cost
                estimated_cost = float(cost["preferred"])
        self.db.execute(
            """
            UPDATE itinerary_segments
            SET poi_id = ?, notes = ?, end_time = ?, estimated_cost = ?, estimate_metadata_json = ?, semantic_metadata_json = ?
            WHERE id = ? AND plan_id = ?
            """,
            (
                poi_id,
                operation.notes or operation.amap_poi.source_note,
                self._format_minutes(self._minutes(segment["start_time"]) + duration),
                estimated_cost,
                json.dumps(metadata, ensure_ascii=False),
                json.dumps(self._selected_semantic_metadata(segment, operation), ensure_ascii=False),
                operation.segment_id,
                plan_id,
            ),
        )

    def _estimate_metadata(self, segment: sqlite3.Row) -> dict[str, Any]:
        try:
            value = json.loads(segment["estimate_metadata_json"] or "{}")
            return value if isinstance(value, dict) else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}

    def _selected_semantic_metadata(self, segment: sqlite3.Row, operation: ItineraryPatchOperation) -> dict[str, Any]:
        try:
            metadata = json.loads(segment["semantic_metadata_json"] or "{}")
            if not isinstance(metadata, dict):
                metadata = {}
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            metadata = {}
        poi = operation.amap_poi
        text = (
            f"{getattr(poi, 'name', '')} {getattr(poi, 'type', '')} {getattr(poi, 'category', '')} {segment['notes']}"
        )
        if not metadata.get("intentType"):
            for intent_type, pattern in (
                ("museum", r"美术馆|艺术博物馆|博物馆|博物院|画院|艺术馆"),
                ("campus_visit", r"大学|学院|高校|校园|校区"),
                ("meal", r"餐厅|饭店|餐饮|午餐|晚餐|早餐|美食"),
                ("night_view", r"夜景|夜游|观景台|天际线"),
                ("park", r"公园|园林|植物园|湿地"),
            ):
                if re.search(pattern, text):
                    metadata["intentType"] = intent_type
                    break
        metadata["groundingStatus"] = "selected"
        metadata["routeAnchor"] = bool(poi and poi.id and poi.longitude is not None and poi.latitude is not None)
        metadata.setdefault("required", bool(metadata.get("intentType")))
        metadata.setdefault("rawNeed", str(segment["notes"] or ""))
        metadata.setdefault("aliases", [])
        if poi and poi.name and poi.name not in metadata["aliases"]:
            metadata["aliases"] = [*metadata["aliases"], poi.name]
        return metadata

    def _plan_budget_tier(self, plan_id: str) -> str:
        row = self.db.execute("SELECT budget_tier FROM itinerary_plans WHERE id = ?", (plan_id,)).fetchone()
        return str(row["budget_tier"] or "unknown") if row else "unknown"

    def _persist_budget_tier(self, plan_id: str, planning_context: Optional[dict]) -> None:
        context = planning_context if isinstance(planning_context, dict) else {}
        requirements = (
            context.get("understoodRequirements") if isinstance(context.get("understoodRequirements"), dict) else {}
        )
        fields = requirements.get("fields") if isinstance(requirements.get("fields"), dict) else {}
        text = str(context.get("effectiveUserMessage") or context.get("latestUserMessage") or "")
        tier = MealGroundingPolicy().budget_tier(fields.get("budget"), text)
        if tier != "unknown":
            self.db.execute("UPDATE itinerary_plans SET budget_tier = ? WHERE id = ?", (tier, plan_id))

    def _hard_constraints_for_snapshot(self, planning_context: Optional[dict]) -> dict:
        context = planning_context if isinstance(planning_context, dict) else {}
        constraints = context.get("hardConstraints")
        if not isinstance(constraints, dict):
            return {}
        campus = constraints.get("campusTier")
        if not isinstance(campus, dict) or not campus.get("value"):
            return {}
        return {"campusTier": dict(campus)}

    def _prepare_poi_candidate_expansions(
        self, plan_id: str, operations: list[ItineraryPatchOperation]
    ) -> dict[str, dict]:
        prepared = self._prepare_expand_area_poi_candidates(plan_id, operations)
        prepared.update(self._prepare_expand_meal_poi_candidates(plan_id, operations))
        return prepared

    def _prepare_expand_area_poi_candidates(
        self, plan_id: str, operations: list[ItineraryPatchOperation]
    ) -> dict[str, dict]:
        prepared: dict[str, dict] = {}
        for operation in operations:
            if operation.op != "expand_area_poi_candidates":
                continue
            segment = self._segment_with_poi(plan_id, operation.segment_id or "")
            if segment is None:
                raise HTTPException(status_code=400, detail="Cannot expand POI candidates without an itinerary segment")
            metadata = self._poi_grounding_metadata(segment)
            if not metadata.get("needsConcretePoi"):
                raise HTTPException(status_code=400, detail="Current segment does not need concrete POI candidates")
            center = self._candidate_search_center(plan_id, segment)
            if center is None:
                raise HTTPException(
                    status_code=400,
                    detail="无法展开具体候选：当前地点和同一天相邻地点都缺少高德坐标。请先搜索或确认一个附近地图锚点。",
                )
            keyword, category = self._concrete_candidate_query(segment, metadata, operation.value)
            city = segment["poi_city"] or self._plan_city(plan_id)
            radius = operation.radius or 1500
            response = MapPoiService().search_nearby(
                city=city,
                longitude=center["longitude"],
                latitude=center["latitude"],
                keyword=keyword,
                category=category,
                radius=radius,
                limit=12,
            )
            if not response.pois:
                raise HTTPException(
                    status_code=400, detail=f"高德地图未返回「{keyword}」附近可替换候选，请换更明确的地点或关键词。"
                )
            prepared[operation.segment_id or ""] = {
                "query": segment["poi_name"],
                "city": city,
                "category": category,
                "candidates": response.pois,
                "segment_id": operation.segment_id,
            }
        return prepared

    def _prepare_expand_meal_poi_candidates(
        self, plan_id: str, operations: list[ItineraryPatchOperation]
    ) -> dict[str, dict]:
        prepared: dict[str, dict] = {}
        for operation in operations:
            if operation.op != "expand_meal_poi_candidates":
                continue
            segment = self._segment_with_poi(plan_id, operation.segment_id or "")
            if segment is None:
                raise HTTPException(
                    status_code=400, detail="Cannot expand meal candidates without an itinerary segment"
                )
            metadata = self._poi_grounding_metadata(segment)
            if not self._meal_segment_allows_candidate_expansion(segment, metadata):
                raise HTTPException(
                    status_code=400, detail="Current meal segment cannot expand nearby dining candidates"
                )
            city = segment["poi_city"] or self._plan_city(plan_id)
            keyword = self._meal_candidate_keyword(segment, operation.value)
            radius = operation.radius or self._meal_search_radius(segment)
            map_service = MapPoiService()
            candidates: list[MapPoiResponse] = []
            provider_errors: list[str] = []
            existing_keys = self._existing_pending_meal_candidate_keys(plan_id, segment["id"])
            centers = self._meal_candidate_search_centers(plan_id, segment)
            for center in centers:
                try:
                    response = map_service.search_nearby(
                        city=city,
                        longitude=center["longitude"],
                        latitude=center["latitude"],
                        keyword=keyword,
                        category="food",
                        radius=radius,
                        limit=12,
                    )
                except HTTPException as error:
                    provider_errors.append(self._http_error_message(error))
                    continue
                for poi in response.pois:
                    route_impact = self._meal_candidate_route_impact(segment, poi, center)
                    self._set_pending_candidate_debug(poi, route_impact, self._meal_candidate_reason(route_impact))
                    candidates.append(poi)
                filtered = self._filter_meal_candidates(candidates, existing_keys=existing_keys)
                if len(filtered) >= 4:
                    break
            filtered_candidates = self._filter_meal_candidates(candidates, existing_keys=existing_keys)
            filtered_candidates = sorted(
                filtered_candidates,
                key=lambda candidate: self._meal_candidate_route_sort_key(candidate),
            )
            if not filtered_candidates:
                if provider_errors:
                    raise HTTPException(status_code=502, detail=provider_errors[-1])
                raise HTTPException(
                    status_code=400,
                    detail=f"前后路线节点附近未返回「{keyword}」可用餐饮候选，可手动输入餐厅或扩大范围。",
                )
            prepared[operation.segment_id or ""] = {
                "query": self._meal_candidate_query_label(segment, keyword),
                "city": city,
                "category": "food",
                "candidates": filtered_candidates[:8],
                "segment_id": operation.segment_id,
            }
        return prepared

    def _expand_area_poi_candidates(
        self, plan_id: str, operation: ItineraryPatchOperation, prepared_expansions: dict[str, dict]
    ) -> None:
        session = self._session_for_plan(plan_id)
        prepared = prepared_expansions.get(operation.segment_id or "")
        if session is None or prepared is None:
            raise HTTPException(
                status_code=400, detail="Prepared POI candidates are missing for this itinerary segment"
            )
        self._insert_pending_poi_candidate(
            session_id=session["id"],
            turn_id=None,
            query=prepared["query"],
            segment_id=prepared["segment_id"],
            city=prepared["city"],
            category=prepared["category"],
            reason="needs_concrete_poi",
            candidates=prepared["candidates"],
        )

    def _expand_meal_poi_candidates(
        self, plan_id: str, operation: ItineraryPatchOperation, prepared_expansions: dict[str, dict]
    ) -> None:
        session = self._session_for_plan(plan_id)
        prepared = prepared_expansions.get(operation.segment_id or "")
        if session is None or prepared is None:
            raise HTTPException(
                status_code=400, detail="Prepared meal candidates are missing for this itinerary segment"
            )
        self._insert_pending_poi_candidate(
            session_id=session["id"],
            turn_id=None,
            query=prepared["query"],
            segment_id=prepared["segment_id"],
            city=prepared["city"],
            category=prepared["category"],
            reason="meal_candidate_expansion",
            candidates=prepared["candidates"],
        )

    def _meal_segment_allows_candidate_expansion(self, segment: sqlite3.Row, metadata: dict) -> bool:
        if segment["kind"] != "meal":
            return False
        status = str(metadata.get("groundingStatus") or "").strip()
        concrete_statuses = {"verified_amap", "agent_selected_candidate", "user_confirmed"}
        if segment["source"] == AMAP_PLACE_SOURCE and segment["amap_id"] and status in concrete_statuses:
            return False
        return (
            status
            in {
                "",
                "optional_waiting",
                "not_required",
                "draft_only",
                "waiting_for_poi_grounding",
                "provider_rate_limited",
                "area_unresolved",
                "functional_poi",
            }
            or not segment["amap_id"]
        )

    def _meal_search_radius(self, segment: sqlite3.Row) -> int:
        mode = str(segment["transport_mode"] or "").lower()
        if mode in {"public_transit", "transit", "bus", "subway", "metro"}:
            return 2500
        return 1800

    def _meal_candidate_keyword(self, segment: sqlite3.Row, override_keyword: Optional[str]) -> str:
        if override_keyword and override_keyword.strip():
            return override_keyword.strip()
        name = str(segment["poi_name"] or "").strip()
        if re.search(r"(咖啡|下午茶|小吃|夜市|火锅|烧烤|素食|清真|海鲜|本帮|川菜|粤菜|湘菜|餐厅|饭店|美食)", name):
            return self._clean_candidate_keyword(name) or "餐厅"
        return "餐厅"

    def _meal_candidate_query_label(self, segment: sqlite3.Row, keyword: str) -> str:
        name = str(segment["poi_name"] or "").strip()
        if name and name not in {"午餐", "晚餐", "早餐", "用餐", "吃饭"}:
            return name
        return f"{name or '用餐时间'}附近{keyword}"

    def _meal_candidate_search_centers(self, plan_id: str, segment: sqlite3.Row) -> list[dict[str, Any]]:
        anchors = [
            row
            for row in self._route_anchor_rows_for_day(plan_id, segment["day_id"])
            if self._valid_coordinate_pair(row["longitude"], row["latitude"])
        ]
        target_order = int(segment["segment_order"] or 0)
        previous_anchor = next(
            (row for row in reversed(anchors) if int(row["segment_order"] or 0) < target_order), None
        )
        next_anchor = next((row for row in anchors if int(row["segment_order"] or 0) > target_order), None)
        centers: list[dict[str, Any]] = []
        if previous_anchor is not None and next_anchor is not None:
            centers.append(
                {
                    "longitude": (float(previous_anchor["longitude"]) + float(next_anchor["longitude"])) / 2,
                    "latitude": (float(previous_anchor["latitude"]) + float(next_anchor["latitude"])) / 2,
                    "searchMode": "midpoint_between_adjacent_anchors",
                    "previousAnchor": previous_anchor,
                    "nextAnchor": next_anchor,
                }
            )
        self._append_unique_center(
            centers,
            previous_anchor,
            search_mode="near_previous_anchor",
            previous_anchor=previous_anchor,
            next_anchor=next_anchor,
        )
        self._append_unique_center(
            centers,
            next_anchor,
            search_mode="near_next_anchor",
            previous_anchor=previous_anchor,
            next_anchor=next_anchor,
        )
        return self._dedupe_centers(centers)

    def _append_unique_center(
        self,
        centers: list[dict[str, Any]],
        row: Optional[sqlite3.Row],
        *,
        search_mode: str,
        previous_anchor: Optional[sqlite3.Row],
        next_anchor: Optional[sqlite3.Row],
    ) -> None:
        if row is None:
            return
        centers.append(
            {
                "longitude": float(row["longitude"]),
                "latitude": float(row["latitude"]),
                "searchMode": search_mode,
                "previousAnchor": previous_anchor,
                "nextAnchor": next_anchor,
            }
        )

    def _dedupe_centers(self, centers: list[dict[str, Any]]) -> list[dict[str, Any]]:
        deduped: list[dict[str, Any]] = []
        seen: set[tuple[float, float]] = set()
        for center in centers:
            key = (round(float(center["longitude"]), 5), round(float(center["latitude"]), 5))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(center)
        return deduped

    def _filter_meal_candidates(
        self,
        candidates: list[MapPoiResponse],
        *,
        existing_keys: Optional[set[str]] = None,
    ) -> list[MapPoiResponse]:
        filtered: list[MapPoiResponse] = []
        seen: set[str] = set()
        existing_keys = existing_keys or set()
        for candidate in candidates:
            dedupe_keys = self._meal_candidate_dedupe_keys(candidate)
            primary_key = next(iter(dedupe_keys), "")
            if not primary_key or dedupe_keys & seen or dedupe_keys & existing_keys:
                continue
            if not self._is_meal_candidate(candidate):
                continue
            seen.update(dedupe_keys)
            filtered.append(candidate)
        return filtered

    def _existing_pending_meal_candidate_keys(self, plan_id: str, segment_id: str) -> set[str]:
        session = self._session_for_plan(plan_id)
        if session is None:
            return set()
        rows = self.db.execute(
            """
            SELECT candidates_json
            FROM amap_poi_candidates
            WHERE session_id = ? AND segment_id = ?
              AND status IN ('pending', 'selected')
            """,
            (session["id"], segment_id),
        ).fetchall()
        keys: set[str] = set()
        for row in rows:
            try:
                payload = json.loads(row["candidates_json"] or "[]")
            except json.JSONDecodeError:
                continue
            for item in payload if isinstance(payload, list) else []:
                if isinstance(item, dict):
                    keys.update(self._meal_candidate_dedupe_keys(item))
        return keys

    def _meal_candidate_dedupe_keys(self, candidate: Any) -> set[str]:
        candidate_id = str(
            getattr(candidate, "id", "") if not isinstance(candidate, dict) else candidate.get("id", "") or ""
        ).strip()
        name = str(
            getattr(candidate, "name", "") if not isinstance(candidate, dict) else candidate.get("name", "") or ""
        )
        address = str(
            getattr(candidate, "address", "") if not isinstance(candidate, dict) else candidate.get("address", "") or ""
        )
        brand = self.meal_diversity_policy.canonical_meal_brand(SimpleNamespace(name=name))
        name_address_key = re.sub(r"\s+", "", f"{name}{address}").casefold()
        keys = {
            f"id:{candidate_id}" if candidate_id else "",
            f"name_address:{name_address_key}" if name_address_key else "",
            f"brand:{brand}" if brand else "",
        }
        return {key for key in keys if key}

    def _meal_candidate_route_impact(
        self, segment: sqlite3.Row, candidate: MapPoiResponse, center: dict[str, Any]
    ) -> dict[str, Any]:
        previous_anchor = center.get("previousAnchor")
        next_anchor = center.get("nextAnchor")
        previous_view = self._route_anchor_view(previous_anchor)
        next_view = self._route_anchor_view(next_anchor)
        score = self.route_insertion_scorer.score(
            previous_view,
            candidate,
            next_view,
            transport_mode=str(segment["transport_mode"] or ""),
        )
        impact = {
            "previousAnchor": getattr(previous_view, "name", "") if previous_view else None,
            "nextAnchor": getattr(next_view, "name", "") if next_view else None,
            "searchMode": center.get("searchMode"),
            "networkVerified": False,
            "decisionRole": "geometry_coarse_ordering_only",
        }
        if score is not None:
            impact.update(
                {
                    "totalDistanceKm": score.total_distance_km,
                    "addedDistanceKm": score.added_distance_km,
                    "estimatedDurationMinutes": score.estimated_duration_minutes,
                    "addedDurationMinutes": score.added_duration_minutes,
                    "detourLevel": score.detour_level,
                    "reason": score.reason,
                }
            )
        else:
            impact.update({"detourLevel": "unknown"})
        return {key: value for key, value in impact.items() if value not in (None, "")}

    def _route_anchor_view(self, row: Optional[sqlite3.Row]) -> Optional[SimpleNamespace]:
        if row is None:
            return None
        return SimpleNamespace(
            name=str(row["poi_name"] or ""),
            latitude=float(row["latitude"]),
            longitude=float(row["longitude"]),
        )

    def _meal_candidate_reason(self, route_impact: dict[str, Any]) -> str:
        previous_anchor = route_impact.get("previousAnchor")
        next_anchor = route_impact.get("nextAnchor")
        level = route_impact.get("detourLevel") or "unknown"
        if route_impact.get("networkVerified") is True:
            if previous_anchor and next_anchor:
                return f"经路线 Provider 核验，位于「{previous_anchor}」与「{next_anchor}」之间；绕路等级：{level}。"
            anchor = previous_anchor or next_anchor
            return (
                f"经路线 Provider 核验，靠近「{anchor}」；绕路等级：{level}。"
                if anchor
                else f"经路线 Provider 核验；绕路等级：{level}。"
            )
        if previous_anchor and next_anchor:
            return (
                f"位于「{previous_anchor}」与「{next_anchor}」邻近范围的餐饮候选；"
                f"直线距离仅用于粗排，实际路线待 Provider 核验（粗排等级：{level}）。"
            )
        anchor = previous_anchor or next_anchor
        if anchor:
            return f"靠近「{anchor}」的餐饮候选；直线距离仅用于粗排，实际路线待 Provider 核验（粗排等级：{level}）。"
        return f"餐饮候选的实际路线待 Provider 核验（直线粗排等级：{level}）。"

    def _set_pending_candidate_debug(
        self, candidate: MapPoiResponse, route_impact: dict[str, Any], reason: str
    ) -> None:
        try:
            object.__setattr__(candidate, "_trip_route_impact", route_impact)
            object.__setattr__(candidate, "_trip_reason", reason)
        except Exception:
            return

    def _meal_candidate_route_sort_key(self, candidate: MapPoiResponse) -> tuple[int, float, str]:
        impact = getattr(candidate, "_trip_route_impact", {}) or {}
        level_rank = {"low": 0, "medium": 1, "high": 2, "unknown": 3, "unacceptable": 4}
        added = impact.get("addedDistanceKm")
        try:
            added_value = float(added)
        except (TypeError, ValueError):
            added_value = 999.0
        return (level_rank.get(str(impact.get("detourLevel") or "unknown"), 3), added_value, candidate.name)

    def _is_meal_candidate(self, candidate: MapPoiResponse) -> bool:
        text = " ".join(
            [
                candidate.name or "",
                candidate.type or "",
                candidate.category or "",
                candidate.address or "",
                candidate.district or "",
            ]
        )
        if candidate.source != AMAP_PLACE_SOURCE:
            return False
        if not self._valid_coordinate_pair(candidate.longitude, candidate.latitude):
            return False
        if MEAL_CANDIDATE_REJECT_RE.search(text):
            return False
        return bool(candidate.category == "food" or MEAL_CANDIDATE_TYPE_RE.search(text))

    def _candidate_search_center(self, plan_id: str, segment: sqlite3.Row) -> Optional[dict]:
        if self._valid_coordinate_pair(segment["longitude"], segment["latitude"]):
            return {"longitude": float(segment["longitude"]), "latitude": float(segment["latitude"])}
        rows = self.db.execute(
            """
            SELECT s.id, s.segment_order, p.longitude, p.latitude
            FROM itinerary_segments s
            JOIN pois p ON p.id = s.poi_id
            WHERE s.plan_id = ? AND s.day_id = ?
            ORDER BY s.segment_order ASC, s.start_time ASC
            """,
            (plan_id, segment["day_id"]),
        ).fetchall()
        target_index = next((index for index, row in enumerate(rows) if row["id"] == segment["id"]), -1)
        if target_index < 0:
            return None
        offsets = []
        for step in range(1, len(rows) + 1):
            offsets.extend([target_index - step, target_index + step])
        for index in offsets:
            if 0 <= index < len(rows):
                row = rows[index]
                if self._valid_coordinate_pair(row["longitude"], row["latitude"]):
                    return {"longitude": float(row["longitude"]), "latitude": float(row["latitude"])}
        return None

    def _concrete_candidate_query(
        self, segment: sqlite3.Row, metadata: dict, override_keyword: Optional[str]
    ) -> tuple[str, str]:
        if override_keyword and override_keyword.strip():
            return override_keyword.strip(), self._category_for_intent(
                metadata.get("intentType"), segment["poi_category"]
            )
        name = str(segment["poi_name"] or "").strip()
        intent = metadata.get("intentType")
        if metadata.get("poiSpecificity") in {"area_poi", "composite_poi"}:
            return self._clean_candidate_keyword(name) or "景点", self._category_for_intent(
                intent, segment["poi_category"]
            )
        if intent in {"dining", "meal"}:
            return "餐厅", "food"
        if intent == "shopping":
            return "购物", "shopping"
        if intent == "rest":
            return "咖啡", "food"
        if intent in {"experience", "night_view", "landmark", "park", "museum", "area_walk", "campus_visit"}:
            return "景点", "scenic"
        return self._clean_candidate_keyword(name) or "景点", self._category_for_intent(intent, segment["poi_category"])

    def _category_for_intent(self, intent: Optional[str], fallback: Optional[str]) -> str:
        if intent in {"dining", "meal"}:
            return "food"
        if intent == "shopping":
            return "shopping"
        if intent in {"experience", "area", "night_view", "landmark", "park", "museum", "area_walk", "campus_visit"}:
            return "scenic"
        return (
            fallback
            if fallback in {"all", "scenic", "food", "experience", "shopping", "lodging", "transport"}
            else "all"
        )

    def _clean_candidate_keyword(self, value: str) -> str:
        cleaned = re.sub(
            r"(周边|附近|周围|一带|区域|商圈|片区|街区|园区|午餐|晚餐|早餐|早饭|中饭|午饭|吃饭|用餐|餐厅|美食|咖啡|下午茶|夜景|休息|购物|漫步)",
            " ",
            value,
        )
        cleaned = re.sub(r"[/／、,，;；]+", " ", cleaned)
        return re.sub(r"\s+", " ", cleaned).strip()

    def _insert_pending_poi_candidate(
        self,
        session_id: str,
        turn_id: Optional[str],
        query: str,
        segment_id: Optional[str],
        city: str,
        category: str,
        reason: str,
        candidates: list[MapPoiResponse],
    ) -> str:
        candidate_id = f"cand_{uuid4().hex[:12]}"
        self.db.execute(
            """
            INSERT INTO amap_poi_candidates (
                id, session_id, turn_id, query, segment_id, city, category, status,
                candidates_json, selected_amap_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                candidate_id,
                session_id,
                turn_id,
                query,
                segment_id,
                city,
                category,
                "pending",
                json.dumps([self._pending_candidate_payload(poi) for poi in candidates], ensure_ascii=False),
                None,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        return candidate_id

    def _pending_candidate_payload(self, poi: MapPoiResponse) -> dict[str, Any]:
        payload = poi.model_dump(by_alias=True)
        route_impact = getattr(poi, "_trip_route_impact", None)
        reason = getattr(poi, "_trip_reason", None)
        if isinstance(route_impact, dict) and route_impact:
            payload["routeImpact"] = route_impact
        if isinstance(reason, str) and reason.strip():
            payload["reason"] = reason.strip()
        return payload

    def _valid_coordinate_pair(self, longitude: object, latitude: object) -> bool:
        try:
            lon = float(longitude)
            lat = float(latitude)
        except (TypeError, ValueError):
            return False
        return lon != 0 and lat != 0

    def _plan_city(self, plan_id: str) -> str:
        row = self.db.execute("SELECT city FROM itinerary_plans WHERE id = ?", (plan_id,)).fetchone()
        city = str(row["city"] or "").strip() if row is not None else ""
        if not city:
            raise HTTPException(
                status_code=409,
                detail="itinerary_plan_city_missing_for_provider_preflight",
            )
        return city

    def _confirm_poi_anchor(self, plan_id: str, segment_id: str) -> None:
        row = self._segment_with_poi(plan_id, segment_id)
        if row is None:
            raise HTTPException(status_code=400, detail=f"Segment not found: {segment_id}")
        metadata = self._poi_grounding_metadata(row)
        if metadata["groundingStatus"] != "routeable_anchor":
            raise HTTPException(
                status_code=400,
                detail="confirm_poi_anchor only supports exact routeable anchors; area/function POIs must be replaced with a concrete AMap candidate first",
            )
        self.db.execute(
            """
            UPDATE pois
            SET source = ?, confidence = MAX(confidence, 0.9), source_note = ?
            WHERE id = ? AND plan_id = ?
            """,
            (
                AMAP_PLACE_SOURCE,
                "用户已确认该高德地图锚点就是目标地点。groundingStatus：user_confirmed；来源：用户手动确认的高德地图地点",
                row["poi_id"],
                plan_id,
            ),
        )

    def _move_segment(self, plan_id: str, operation: ItineraryPatchOperation) -> None:
        segment = self._segment(plan_id, operation.segment_id or "")
        source_day_id = segment["day_id"]
        target_day_id = operation.target_day_id or operation.day_id or source_day_id
        start_time = operation.start_time or operation.value or segment["start_time"]
        duration = self._segment_duration(segment)
        end_time = self._format_minutes(self._minutes(start_time) + duration)
        self.db.execute(
            """
            UPDATE itinerary_segments
            SET day_id = ?, start_time = ?, end_time = ?
            WHERE id = ? AND plan_id = ?
            """,
            (target_day_id, start_time, end_time, operation.segment_id, plan_id),
        )
        self._reorder_day_segments(plan_id, source_day_id)
        if target_day_id != source_day_id:
            self._reorder_day_segments(plan_id, target_day_id)

    def _reorder_segments(self, plan_id: str, day_id: str, ordered_segment_ids: list[str]) -> None:
        current_segments = self._segments_for_day(plan_id, day_id)
        segments_by_id = {row["id"]: row for row in current_segments}
        if not current_segments:
            return
        current_start = self._minutes(current_segments[0]["start_time"])
        gaps_after_slot: list[int] = []
        for current, next_segment in zip(current_segments, current_segments[1:]):
            gaps_after_slot.append(
                max(0, self._minutes(next_segment["start_time"]) - self._minutes(current["end_time"]))
            )
        for index, segment_id in enumerate(ordered_segment_ids, start=1):
            segment = segments_by_id[segment_id]
            duration = max(15, self._segment_duration(segment))
            next_end = current_start + duration
            self.db.execute(
                """
                UPDATE itinerary_segments
                SET segment_order = ?, start_time = ?, end_time = ?
                WHERE id = ? AND plan_id = ? AND day_id = ?
                """,
                (
                    index,
                    self._format_minutes(current_start),
                    self._format_minutes(next_end),
                    segment_id,
                    plan_id,
                    day_id,
                ),
            )
            if index - 1 < len(gaps_after_slot):
                current_start = next_end + gaps_after_slot[index - 1]

    def _insert_resolved_amap_poi(
        self, plan_id: str, poi: Optional[MapPoiResponse], *, source_type: str = "manual"
    ) -> str:
        if poi is None:
            raise HTTPException(status_code=400, detail="Resolved AMap POI is required")
        poi_id = f"poi_{uuid4().hex[:12]}"
        source_note = self._source_note_for_inserted_amap_poi(poi, source_type)
        self.db.execute(
            """
            INSERT INTO pois (
                id, plan_id, name, city, category, latitude, longitude,
                photo_url, source, confidence, amap_id, type, district, address,
                source_note, source_url, photos_json, provider_type_code,
                tags_json, source_claims_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                poi_id,
                plan_id,
                poi.name,
                poi.city,
                poi.category,
                poi.latitude,
                poi.longitude,
                poi.photos[0].url if poi.photos else None,
                poi.source,
                poi.confidence,
                poi.id,
                poi.type,
                poi.district,
                poi.address,
                source_note,
                None,
                json.dumps([photo.model_dump() for photo in poi.photos], ensure_ascii=False),
                poi.provider_type_code,
                json.dumps(poi.tags, ensure_ascii=False),
                json.dumps(poi.source_claims, ensure_ascii=False),
            ),
        )
        return poi_id

    def _source_note_for_inserted_amap_poi(self, poi: MapPoiResponse, source_type: str) -> str:
        note = str(poi.source_note or "")
        if source_type == "agent" or "groundingStatus：" in note:
            return note
        suffix = "groundingStatus：user_confirmed；来源：用户手动确认的高德地图地点"
        return f"{note}；{suffix}" if note else suffix

    def _insert_skeleton_poi(
        self,
        plan_id: str,
        title: str,
        *,
        category: str = "pending",
        source_note: str = "用户新增的日程占位，等待 Agent 或地图 POI 确认。",
    ) -> str:
        plan = self.db.execute("SELECT city FROM itinerary_plans WHERE id = ?", (plan_id,)).fetchone()
        city = str(plan["city"] or "").strip() if plan is not None else ""
        if not city:
            raise HTTPException(
                status_code=409,
                detail="itinerary_plan_city_missing_for_skeleton_poi",
            )
        poi_name = (title or "").strip() or "待定景点/活动"
        poi_id = f"poi_{uuid4().hex[:12]}"
        self.db.execute(
            """
            INSERT INTO pois (
                id, plan_id, name, city, category, latitude, longitude,
                photo_url, source, confidence, amap_id, type, district, address,
                source_note, source_url, photos_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                poi_id,
                plan_id,
                poi_name,
                city,
                category,
                None,
                None,
                None,
                SKELETON_POI_SOURCE,
                0.0,
                None,
                "待定",
                "",
                "",
                source_note,
                None,
                "[]",
            ),
        )
        return poi_id

    def _next_segment_start_time(self, plan_id: str, day_id: str) -> str:
        row = self.db.execute(
            """
            SELECT end_time FROM itinerary_segments
            WHERE plan_id = ? AND day_id = ?
            ORDER BY start_time DESC, segment_order DESC
            LIMIT 1
            """,
            (plan_id, day_id),
        ).fetchone()
        if row is None:
            return "09:30"
        next_start = self._minutes(row["end_time"]) + 30
        if next_start + 30 > 24 * 60:
            return "09:30"
        return self._format_minutes(next_start)

    def _next_segment_order(self, plan_id: str, day_id: str) -> int:
        row = self.db.execute(
            "SELECT COALESCE(MAX(segment_order), 0) AS max_order FROM itinerary_segments WHERE plan_id = ? AND day_id = ?",
            (plan_id, day_id),
        ).fetchone()
        return int(row["max_order"]) + 1

    def _reorder_day_segments(self, plan_id: str, day_id: str) -> None:
        rows = self.db.execute(
            """
            SELECT id FROM itinerary_segments
            WHERE plan_id = ? AND day_id = ?
            ORDER BY start_time ASC, segment_order ASC
            """,
            (plan_id, day_id),
        ).fetchall()
        for index, row in enumerate(rows, start=1):
            self.db.execute(
                "UPDATE itinerary_segments SET segment_order = ? WHERE id = ? AND plan_id = ?",
                (index, row["id"], plan_id),
            )

    def _recalculate_plan_totals(self, plan_id: str) -> None:
        day_rows = self.db.execute("SELECT id FROM itinerary_days WHERE plan_id = ?", (plan_id,)).fetchall()
        total = 0.0
        for day in day_rows:
            row = self.db.execute(
                "SELECT COALESCE(SUM(estimated_cost), 0) AS total FROM itinerary_segments WHERE plan_id = ? AND day_id = ?",
                (plan_id, day["id"]),
            ).fetchone()
            day_total = float(row["total"])
            total += day_total
            self.db.execute(
                "UPDATE itinerary_days SET total_estimated_cost = ? WHERE id = ? AND plan_id = ?",
                (day_total, day["id"], plan_id),
            )
        self.db.execute(
            "UPDATE itinerary_plans SET budget_estimate = ?, updated_at = ? WHERE id = ?",
            (total, datetime.now(timezone.utc).isoformat(), plan_id),
        )

    def _segment_duration(self, segment: sqlite3.Row) -> int:
        return self._minutes(segment["end_time"]) - self._minutes(segment["start_time"])

    def _valid_clock(self, value: str) -> bool:
        return re.match(r"^([01]\d|2[0-3]):[0-5]\d$", value) is not None

    def _minutes(self, value: str) -> int:
        hours, minutes = value.split(":")
        return int(hours) * 60 + int(minutes)

    def _format_minutes(self, value: int) -> str:
        hours = value // 60
        minutes = value % 60
        return f"{hours:02d}:{minutes:02d}"

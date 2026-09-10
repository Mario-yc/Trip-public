import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import PlainTextResponse

from src.api.schemas.itineraries import (
    ItineraryEditRequest,
    ItineraryGenerateRequest,
    LocalReplanSuggestionRequest,
    ItineraryPlanEnvelope,
    PlanComparisonResponse,
    RouteOptimizeRequest,
    RouteSelectRequest,
    SavedItineraryVersionResponse,
    SavedItineraryVersionsResponse,
)
from src.api.schemas.planning import PlanningRunResponse
from src.api.schemas.itinerary_patches import (
    ItineraryPatchOperation,
    ItineraryPatchRequest,
    ItineraryPatchResponse,
    ItineraryRestoreVersionRequest,
    ItineraryRestoreVersionResponse,
)
from src.core.database import get_db
from src.services.itinerary_service import ItineraryService
from src.services.itinerary_patch_service import ItineraryPatchService
from src.services.itinerary_export_service import ItineraryExportService
from src.services.itinerary_snapshot_service import ItinerarySnapshotService
from src.services.map_poi_service import MapPoiService
from src.services.plan_comparison_service import PlanComparisonService
from src.services.planning_run_service import PlanningRunService
from src.services.ticket_service import TicketService
from src.services.segment_visit_facts_service import SegmentVisitFactsService


router = APIRouter(prefix="/itineraries", tags=["itineraries"])


@router.post("/generate", response_model=ItineraryPlanEnvelope)
def generate_itinerary(
    payload: ItineraryGenerateRequest, db: sqlite3.Connection = Depends(get_db)
) -> ItineraryPlanEnvelope:
    plan = ItineraryService(db).generate_for_inspiration(
        payload.inspiration_set_id,
        payload.city,
        preference_profile_id=payload.preference_profile_id,
        preference_summary=payload.preference_summary,
        planning_context=payload.planning_context,
        date_range=payload.date_range,
    )
    planning_run = PlanningRunService(db).create_run(
        "itinerary_generate",
        user_input=str((payload.planning_context or {}).get("currentUserMessage") or ""),
        preference_summary=payload.preference_summary or "",
        itinerary=plan,
        final_summary=(
            f"已生成 {plan.title}；路线状态为 pending_provider_verification，仍待统一路线合同与 Provider 矩阵核验。"
        ),
    )
    db.commit()
    return ItineraryPlanEnvelope(plan=plan, planningRun=planning_run)


@router.post("/compare", response_model=PlanComparisonResponse)
def compare_itineraries(
    payload: ItineraryGenerateRequest, db: sqlite3.Connection = Depends(get_db)
) -> PlanComparisonResponse:
    return PlanComparisonService(db).generate(
        payload.inspiration_set_id,
        payload.city,
        date_range=payload.date_range,
        preference_profile_id=payload.preference_profile_id,
        preference_summary=payload.preference_summary,
        planning_context=payload.planning_context,
    )


@router.get("/{plan_id}", response_model=ItineraryPlanEnvelope)
def get_itinerary(plan_id: str, db: sqlite3.Connection = Depends(get_db)) -> ItineraryPlanEnvelope:
    plan = ItineraryService(db).get_plan(plan_id)
    return ItineraryPlanEnvelope(plan=plan, planningRun=None)


@router.post("/{plan_id}/versions/{version_id}/save", response_model=SavedItineraryVersionResponse)
def save_itinerary_version(
    plan_id: str,
    version_id: str,
    db: sqlite3.Connection = Depends(get_db),
) -> SavedItineraryVersionResponse:
    return ItineraryExportService(db).save_version(plan_id, version_id)


@router.get("/{plan_id}/versions/saved", response_model=SavedItineraryVersionsResponse)
def list_saved_itinerary_versions(
    plan_id: str,
    db: sqlite3.Connection = Depends(get_db),
) -> SavedItineraryVersionsResponse:
    return SavedItineraryVersionsResponse(savedVersions=ItineraryExportService(db).list_saved_versions(plan_id))


@router.get("/{plan_id}/export")
def export_itinerary(
    plan_id: str,
    format: str = Query(default="json", pattern="^(json|markdown)$"),
    db: sqlite3.Connection = Depends(get_db),
):
    service = ItineraryExportService(db)
    if format == "markdown":
        return PlainTextResponse(service.export_markdown(plan_id), media_type="text/markdown; charset=utf-8")
    return service.export_json(plan_id)


@router.post("/{plan_id}/pois/refresh-unfinished", response_model=ItineraryPlanEnvelope)
def refresh_unfinished_pois(plan_id: str, db: sqlite3.Connection = Depends(get_db)) -> ItineraryPlanEnvelope:
    service = ItineraryService(db)
    warnings = service.refresh_unfinished_pois(plan_id)
    db.commit()
    plan = service.get_plan(plan_id)
    plan.route_warnings = warnings
    planning_run = PlanningRunService(db).create_run(
        "poi_grounding_retry",
        user_input="用户显式重试未完成的 POI 地图匹配。",
        preference_summary="",
        itinerary=plan,
        final_summary="已仅重试 draft_only POI，并按受影响路段局部刷新路线。",
    )
    db.commit()
    return ItineraryPlanEnvelope(plan=plan, planningRun=planning_run)


@router.post("/{plan_id}/tickets/refresh", response_model=ItineraryPlanEnvelope)
def refresh_itinerary_tickets(plan_id: str, db: sqlite3.Connection = Depends(get_db)) -> ItineraryPlanEnvelope:
    TicketService(db).refresh_for_plan(plan_id)
    plan = ItineraryService(db).get_plan(plan_id)
    planning_run = PlanningRunService(db).create_run(
        "ticket_lookup_refresh",
        user_input="用户显式刷新票务/预约状态。",
        preference_summary="",
        itinerary=plan,
        final_summary="已重新查询票务/预约来源，并更新来源可信度与 fallback 状态；未据此声明路线可行。",
    )
    db.commit()
    return ItineraryPlanEnvelope(plan=plan, planningRun=planning_run)


@router.post("/{plan_id}/visit-facts/refresh", response_model=ItineraryPlanEnvelope)
def refresh_itinerary_visit_facts(
    plan_id: str,
    db: sqlite3.Connection = Depends(get_db),
) -> ItineraryPlanEnvelope:
    SegmentVisitFactsService(db).refresh_for_plan(plan_id)
    plan = ItineraryService(db).get_plan(plan_id)
    return ItineraryPlanEnvelope(plan=plan, planningRun=None)


@router.post("/{plan_id}/local-replan/suggestions", response_model=PlanningRunResponse)
def suggest_local_replan(
    plan_id: str,
    payload: LocalReplanSuggestionRequest,
    db: sqlite3.Connection = Depends(get_db),
) -> PlanningRunResponse:
    plan = ItineraryService(db).get_plan(plan_id)
    planning_run = PlanningRunService(db).create_run(
        "local_replan_suggestion",
        user_input=payload.user_input,
        preference_summary=payload.preference_summary,
        itinerary=plan,
        final_summary="已生成局部优化建议，等待用户确认后再应用。",
    )
    db.commit()
    return planning_run


@router.patch("/{plan_id}", response_model=ItineraryPlanEnvelope)
def edit_itinerary(
    plan_id: str,
    payload: ItineraryEditRequest,
    response: Response,
    db: sqlite3.Connection = Depends(get_db),
) -> ItineraryPlanEnvelope:
    response.headers["Deprecation"] = "true"
    response.headers["Link"] = f'</api/itineraries/{plan_id}/patch>; rel="successor-version"'
    if payload.operation != "replace_transport_mode":
        raise HTTPException(status_code=400, detail=f"Unsupported legacy itinerary edit operation: {payload.operation}")
    result = ItineraryPatchService(db).apply_patch(
        plan_id,
        [
            ItineraryPatchOperation(
                op=payload.operation,
                segmentId=payload.segment_id,
                value=payload.value,
            )
        ],
        source_type="legacy_edit",
        base_version_id=payload.base_version_id,
        preference_summary=payload.preference_summary,
        planning_context=payload.planning_context,
    )
    planning_run = PlanningRunService(db).create_run(
        "itinerary_edit",
        user_input=f"用户修改行程：{payload.operation}",
        preference_summary=payload.preference_summary,
        itinerary=result.itinerary,
        itinerary_version_id=result.version.id,
        understood_requirements=(payload.planning_context or {}).get("understoodRequirements"),
        final_summary=(
            "已保存用户手动修改；天气、票务和偏好冲突已更新，"
            "路线状态为 pending_provider_verification，尚未声明顺路或完成可行性检查。"
        ),
    )
    ItineraryPatchService(db).attach_planning_run(result.patch.id, planning_run.id)
    db.commit()
    return ItineraryPlanEnvelope(plan=result.itinerary, planningRun=planning_run)


@router.post("/{plan_id}/patch", response_model=ItineraryPatchResponse)
def patch_itinerary(
    plan_id: str, payload: ItineraryPatchRequest, db: sqlite3.Connection = Depends(get_db)
) -> ItineraryPatchResponse:
    patch_service = ItineraryPatchService(db)
    operations = patch_service.rebind_public_amap_poi_identities(
        plan_id,
        payload.operations,
        planning_context=payload.planning_context,
        map_poi_service=MapPoiService(),
    )
    result = patch_service.apply_patch(
        plan_id,
        operations,
        # The public API's source label is audit data, never a capability.
        # In particular, a caller cannot claim to be an Agent/timeline writer
        # in order to alter route-matrix validation.
        source_type="manual",
        base_version_id=payload.base_version_id,
        source_turn_id=payload.source_turn_id,
        preference_summary=payload.preference_summary,
        planning_context=payload.planning_context,
    )
    if payload.source_type == "local_replan":
        planning_run = PlanningRunService(db).create_run(
            "local_replan_apply",
            user_input="用户确认应用局部优化建议。",
            preference_summary=payload.preference_summary,
            itinerary=result.itinerary,
            itinerary_version_id=result.version.id,
            final_summary=(
                "已按用户确认应用局部优化；路线状态为 pending_provider_verification，固定距离/时长阈值仅供诊断。"
            ),
        )
        ItineraryPatchService(db).attach_planning_run(result.patch.id, planning_run.id)
        result.planning_run = planning_run
        db.commit()
    return result


@router.post("/{plan_id}/routes/{route_option_id}/select", response_model=ItineraryPatchResponse)
def select_itinerary_route(
    plan_id: str,
    route_option_id: str,
    payload: RouteSelectRequest,
    db: sqlite3.Connection = Depends(get_db),
) -> ItineraryPatchResponse:
    result = ItineraryPatchService(db).select_route(
        plan_id,
        route_option_id,
        base_version_id=payload.base_version_id,
        planning_context=payload.planning_context,
    )
    planning_run = PlanningRunService(db).create_run(
        "route_select",
        user_input="用户确认切换路线方案。",
        preference_summary=payload.preference_summary,
        itinerary=result.itinerary,
        itinerary_version_id=result.version.id,
        final_summary="已保存用户确认的路线选择，并重新检查路线、拥挤、票务和天气风险。",
    )
    ItineraryPatchService(db).attach_planning_run(result.patch.id, planning_run.id)
    result.planning_run = planning_run
    db.commit()
    return result


@router.post("/{plan_id}/routes/optimize", response_model=ItineraryPatchResponse)
def optimize_itinerary_routes(
    plan_id: str,
    payload: RouteOptimizeRequest,
    db: sqlite3.Connection = Depends(get_db),
) -> ItineraryPatchResponse:
    result = ItineraryPatchService(db).optimize_routes(
        plan_id,
        base_version_id=payload.base_version_id,
        preference_summary=payload.preference_summary,
        planning_context=payload.planning_context,
        day_id=payload.day_id,
        optimization_objective=payload.optimization_objective,
    )
    metadata = result.patch.metadata.get("routeOptimization", {}) if result.patch.metadata else {}
    changed_count = int(metadata.get("changedCount") or 0) if isinstance(metadata, dict) else 0
    objective = (
        str(metadata.get("objective") or payload.optimization_objective)
        if isinstance(metadata, dict)
        else payload.optimization_objective
    )
    objective_label = {"fastest": "时间最短", "cheapest": "费用最少", "balanced": "综合最优"}.get(objective, "综合最优")
    planning_run = PlanningRunService(db).create_run(
        "route_optimize",
        user_input=f"用户点击优化路线：{objective_label}。",
        preference_summary=payload.preference_summary,
        itinerary=result.itinerary,
        itinerary_version_id=result.version.id,
        understood_requirements={
            "routeOptimization": metadata,
            "optimizationObjective": objective,
            "schedulePolicy": (result.patch.metadata or {}).get("schedulePolicy"),
            "scheduleUpdatedCount": (result.patch.metadata or {}).get("scheduleUpdatedCount", 0),
        },
        final_summary=(
            f"已按“{objective_label}”自动切换 {changed_count} 段路线，并重新排时。"
            if changed_count
            else f"当前路线已符合“{objective_label}”目标。"
        ),
    )
    ItineraryPatchService(db).attach_planning_run(result.patch.id, planning_run.id)
    result.planning_run = planning_run
    db.commit()
    return result


@router.post("/{plan_id}/restore-version", response_model=ItineraryRestoreVersionResponse)
def restore_itinerary_version(
    plan_id: str, payload: ItineraryRestoreVersionRequest, db: sqlite3.Connection = Depends(get_db)
) -> ItineraryRestoreVersionResponse:
    try:
        snapshot, version = ItinerarySnapshotService(db).restore_existing_version(
            plan_id,
            payload.version_id,
            reason=payload.reason,
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return ItineraryRestoreVersionResponse(
        itinerary=ItineraryService(db).get_plan(plan_id),
        version=version,
    )

import { type CSSProperties, type DragEvent, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, apiClient, type AgentSession, ItineraryPatchOperation, ItineraryPlan, type MapPoi, type PendingPoiCandidate, type PendingTimelineSlot, RouteOption } from "../../services/apiClient";
import { effectivePreferenceMemoryText } from "../preferences/PreferenceSummaryCard";
import { plannerStore } from "../../state/plannerStore";
import { isCurrentVersionedWrite } from "../../state/versionedWriteGuard";
import { RiskSignals } from "./RiskSignals";
import { isRouteAnchorSegment } from "./routeAnchors";
import {
  calculateDayTotals,
  calculateTripTotals,
  authoritativeBudgetSummary,
  buildItineraryAgentContext,
  createEditableDays,
  createEmptyDay,
  createSkeletonSegment,
  EditableDay,
  EditableSegment,
  formatDistance,
  formatDuration,
  validateDayTitle,
  validateSegmentStartTime,
  validateTripTitle
} from "./itineraryWorkspace";
import { buildRouteLegColorMap, routeLegColor, type RouteColorMap, routeLegKey } from "./routeVisuals";
import { groundingStatusLabelForSegmentFields } from "./timelineLabels";

const TIMELINE_TIME_PILL_LAYOUT_STYLE: CSSProperties = {
  gridColumn: 2,
  gridRow: "1 / 5",
  position: "relative",
  zIndex: 1
};

const VISIT_FACTS_LAYOUT_STYLE: CSSProperties = {
  gridColumn: "2 / -1",
  minWidth: 0,
  width: "100%"
};

type DailyTimelineProps = {
  plan: ItineraryPlan | null;
  selectedDayNumber: number;
  selectedSegmentId: string | null;
  onSelectSegment: (segmentId: string) => void;
  onSelectPendingSlot?: (slot: PendingTimelineSlot) => void;
  readOnly?: boolean;
  /** @deprecated Transport mode is edited by the dedicated route controls; retained for existing embedders. */
  onTransportChange?: (segmentId: string, value: string) => void;
};

type EditingState = {
  kind: "trip" | "day" | "time" | null;
  id?: string;
  value: string;
};

type RouteSelectionSnapshot = {
  id: string;
  fromSegmentId?: string | null;
  toSegmentId?: string | null;
  fromPoiId: string;
  toPoiId: string;
  mode: string;
  transportMode: string;
};

type RouteDisplayGroup = {
  id: string;
  label: string;
  routeIds: string[];
  representativeRouteId: string;
  selectedRawRouteId?: string;
  durationMinutes: number;
  distanceMeters: number;
  costAmount: number;
  modes: string[];
  rawRoutes: RouteOption[];
  representative: RouteOption;
  isSelected: boolean;
  error?: Record<string, unknown> | null;
};

type DragDropPosition = "before" | "after";
type RouteOptimizationObjective = "balanced" | "fastest" | "cheapest";

type DragDropTarget = {
  segmentId: string;
  position: DragDropPosition;
};

export function DailyTimeline({
  plan,
  selectedSegmentId,
  onSelectSegment,
  onSelectPendingSlot,
  readOnly = false
}: DailyTimelineProps) {
  const [tripTitle, setTripTitle] = useState("");
  const [days, setDays] = useState<EditableDay[]>([]);
  const [editing, setEditing] = useState<EditingState>({ kind: null, value: "" });
  const [errorMessage, setErrorMessage] = useState("");
  const [successMessage, setSuccessMessage] = useState("");
  const [isSaving, setIsSaving] = useState(false);
  const [savingRouteId, setSavingRouteId] = useState<string | null>(null);
  const [isOptimizingRoutes, setIsOptimizingRoutes] = useState(false);
  const [optimizeMenuOpen, setOptimizeMenuOpen] = useState(false);
  const [expandedRouteLegs, setExpandedRouteLegs] = useState<Set<string>>(new Set());
  const [collapsedRouteLegs, setCollapsedRouteLegs] = useState<Set<string>>(new Set());
  const [draggedSegmentId, setDraggedSegmentId] = useState<string | null>(null);
  const [dragDropTarget, setDragDropTarget] = useState<DragDropTarget | null>(null);
  const previousPlan = useRef<ItineraryPlan | null>(null);
  const routeColorMap = useMemo(() => buildRouteLegColorMap(plan?.days ?? [], plan?.routeOptions ?? []), [plan?.days, plan?.routeOptions]);

  useEffect(() => {
    const priorPlan = previousPlan.current;
    const planChanged = (priorPlan?.id ?? null) !== (plan?.id ?? null);
    previousPlan.current = plan;
    const initialDays = createEditableDays(plan);
    setDays(initialDays);
    setTripTitle(plan?.title ?? `${plan?.city ?? "北京"} ${initialDays.length} 日行程`);
    setEditing((current) => {
      // Facts/readback may replace the plan object while the user is typing.
      // Retain the draft only while its authoritative time and target are unchanged.
      if (!planChanged && current.kind === "time") {
        const priorDay = priorPlan?.days.find((day) => day.segments.some((segment) => segment.id === current.id));
        const before = priorDay?.segments.find((segment) => segment.id === current.id);
        const after = plan?.days.find((day) => day.id === priorDay?.id)?.segments.find((segment) => segment.id === current.id);
        if (before && after && before.poi.id === after.poi.id && before.startTime === after.startTime && before.endTime === after.endTime) {
          return current;
        }
      }
      return { kind: null, value: "" };
    });
    setErrorMessage("");
    if (planChanged) {
      setSuccessMessage("");
      setExpandedRouteLegs(new Set());
      setCollapsedRouteLegs(new Set());
      setOptimizeMenuOpen(false);
    }
  }, [plan]);

  useEffect(() => {
    if (!readOnly) return;
    setEditing({ kind: null, value: "" });
    setOptimizeMenuOpen(false);
    setDraggedSegmentId(null);
    setDragDropTarget(null);
  }, [readOnly]);

  useEffect(() => {
    const copyTripTitle = editing.kind === "trip" ? editing.value : tripTitle;
    const copyDays = editing.kind === "day" && editing.id
      ? days.map((day) => (day.id === editing.id ? { ...day, dayTitle: editing.value } : day))
      : editing.kind === "time" && editing.id
        ? days.map((day) => ({
            ...day,
            segments: day.segments.map((segment) =>
              segment.id === editing.id ? { ...segment, startTime: editing.value } : segment
            )
          }))
        : days;
    plannerStore.setState({
      itineraryAgentContext: plan ? buildItineraryAgentContext(tripTitle, days) : null,
      timelineCopyText: plan ? formatTimelineForCopy(plan, copyTripTitle, copyDays) : ""
    });
  }, [days, editing, plan, tripTitle]);

  const ticketResults = useMemo(() => plan?.ticketLookupResults ?? [], [plan?.ticketLookupResults]);
  const ticketBySegment = useMemo(() => new Map(ticketResults.map((ticket) => [ticket.segmentId, ticket])), [ticketResults]);
  const tripTotals = useMemo(() => calculateTripTotals(days), [days]);
  const budgetSummary = useMemo(
    () => plan ? authoritativeBudgetSummary(plan, tripTotals.estimatedCost) : null,
    [plan, tripTotals.estimatedCost]
  );
  const routeWarningSummary = summarizeRouteWarnings(plan, plan?.routeWarnings ?? plannerStore.getSnapshot().routeWarnings);
  const pendingPoiCandidates = plannerStore.getSnapshot().pendingPoiCandidates;

  if (!plan || plan.days.length === 0) {
    return (
      <section aria-label="Daily itinerary timeline content">
        <h2>每日时间轴</h2>
        <p>行程时间轴待生成。</p>
      </section>
    );
  }

  function beginTripTitleEdit() {
    setEditing({ kind: "trip", value: tripTitle });
    setErrorMessage("");
  }

  async function confirmTripTitle() {
    if (readOnly) return;
    const validation = validateTripTitle(editing.value);
    if (validation) {
      setErrorMessage(validation);
      return;
    }
    await applyTimelinePatch([{ op: "replace_trip_title", value: editing.value.trim() }]);
  }

  function beginDayTitleEdit(day: EditableDay) {
    setEditing({ kind: "day", id: day.id, value: day.dayTitle });
    setErrorMessage("");
  }

  async function confirmDayTitle(dayId: string) {
    if (readOnly) return;
    const validation = validateDayTitle(editing.value);
    if (validation) {
      setErrorMessage(validation);
      return;
    }
    await applyTimelinePatch([{ op: "replace_day_title", dayId, value: editing.value.trim() }]);
  }

  function beginTimeEdit(segmentId: string, startTime: string) {
    setEditing({ kind: "time", id: segmentId, value: startTime });
    setErrorMessage("");
  }

  async function confirmTimeEdit(dayId: string, segmentId: string) {
    if (readOnly) return;
    const day = days.find((item) => item.id === dayId);
    if (!day) {
      setErrorMessage("未找到当前 Day。");
      return;
    }
    const validation = validateSegmentStartTime(day, segmentId, editing.value);
    if (validation) {
      setErrorMessage(validation);
      return;
    }
    await applyTimelinePatch([{ op: "replace_segment_start_time", segmentId, value: editing.value }]);
  }

  function cancelEdit() {
    setEditing({ kind: null, value: "" });
    setErrorMessage("");
  }

  function toggleDay(dayId: string) {
    setDays((current) => current.map((day) => (day.id === dayId ? { ...day, collapsed: !day.collapsed } : day)));
  }

  async function addSegment(dayId: string) {
    if (readOnly) return;
    const day = days.find((item) => item.id === dayId);
    if (!day) {
      setErrorMessage("未找到当前 Day。");
      return;
    }
    const selectedMapPoi = plannerStore.getSnapshot().selectedMapPoi;
    if (!selectedMapPoi) {
      setErrorMessage("请先在地图中选择一个高德 POI，再添加到行程。");
      return;
    }
    const skeleton = createSkeletonSegment(day.id, day.dayNumber, day.segments[day.segments.length - 1]);
    await applyTimelinePatch([
      {
        op: "add_segment",
        dayId,
        startTime: skeleton.startTime,
        title: selectedMapPoi.name,
        notes: skeleton.agentNotes,
        durationMinutes: skeleton.durationMinutes,
        amapPoi: selectedMapPoi
      }
    ]);
  }

  async function addDay() {
    if (readOnly) return;
    const nextDay = createEmptyDay(days.length + 1);
    await applyTimelinePatch([{ op: "add_day", title: nextDay.dayTitle }]);
  }

  function beginSegmentDrag(segmentId: string, event: DragEvent<HTMLButtonElement>) {
    setDraggedSegmentId(segmentId);
    setDragDropTarget(null);
    if (event.dataTransfer) {
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", segmentId);
    }
  }

  function dragOverSegment(segmentId: string, event: DragEvent<HTMLElement>) {
    if (!draggedSegmentId) {
      return;
    }
    event.preventDefault();
    if (event.dataTransfer) {
      event.dataTransfer.dropEffect = "move";
    }
    setDragDropTarget({ segmentId, position: dragPositionForEvent(event) });
  }

  async function dropSegment(day: EditableDay, targetSegmentId: string, event: DragEvent<HTMLElement>) {
    if (readOnly) return;
    event.preventDefault();
    const sourceSegmentId = draggedSegmentId ?? event.dataTransfer?.getData("text/plain");
    const dropTarget =
      dragDropTarget?.segmentId === targetSegmentId
        ? dragDropTarget
        : { segmentId: targetSegmentId, position: dragPositionForEvent(event) };
    setDraggedSegmentId(null);
    setDragDropTarget(null);
    if (!sourceSegmentId || sourceSegmentId === targetSegmentId) {
      return;
    }
    const sourceIndex = day.segments.findIndex((segment) => segment.id === sourceSegmentId);
    const targetIndex = day.segments.findIndex((segment) => segment.id === targetSegmentId);
    if (sourceIndex < 0 || targetIndex < 0) {
      setErrorMessage("暂只支持同一天内拖拽排序。");
      return;
    }
    const reorderedSegments = [...day.segments];
    const [moved] = reorderedSegments.splice(sourceIndex, 1);
    let insertIndex = targetIndex + (dropTarget.position === "after" ? 1 : 0);
    if (sourceIndex < insertIndex) {
      insertIndex -= 1;
    }
    reorderedSegments.splice(insertIndex, 0, moved);
    const previousDays = days;
    setDays((current) => current.map((item) => (item.id === day.id ? { ...item, segments: reorderedSegments } : item)));
    await applyReorderPatch(day.id, reorderedSegments.map((segment) => segment.id), previousDays, sourceSegmentId);
  }

  function dragPositionForEvent(event: DragEvent<HTMLElement>): DragDropPosition {
    const rect = event.currentTarget.getBoundingClientRect();
    return event.clientY < rect.top + rect.height / 2 ? "before" : "after";
  }

  async function applyReorderPatch(dayId: string, orderedSegmentIds: string[], previousDays: EditableDay[], movedSegmentId: string) {
    if (!plan) {
      setErrorMessage("行程尚未生成，无法保存排序。");
      setDays(previousDays);
      return;
    }
    setIsSaving(true);
    setErrorMessage("");
    setSuccessMessage("");
    try {
      const snapshot = plannerStore.getSnapshot();
      const baseVersionId = snapshot.activeVersionId;
      const result = await apiClient.patchItinerary(plan.id, {
        sourceType: "manual",
        baseVersionId: baseVersionId ?? null,
        preferenceSummary: effectivePreferenceFromSnapshot(snapshot),
        planningContext: buildTimelinePlanningContext(snapshot, plan, {
          selectedSegmentId: movedSegmentId,
          patchIntent: "reorder_segments"
        }),
        operations: [{ op: "reorder_segments", dayId, orderedSegmentIds }]
      });
      if (!isCurrentVersionedWrite(baseVersionId)) {
        setDays(createEditableDays(plannerStore.getSnapshot().itineraryPlan ?? plan));
        return;
      }
      const nextDays = createEditableDays(result.itinerary);
      plannerStore.setState({
        agentSession: snapshot.agentSession
          ? {
              ...snapshot.agentSession,
              activeVersionId: result.version.id,
              itinerary: result.itinerary,
              pendingPoiCandidates: result.pendingPoiCandidates
            }
          : snapshot.agentSession,
        itineraryPlan: result.itinerary,
        activeVersionId: result.version.id,
        pendingPoiCandidates: result.pendingPoiCandidates,
        lastPatchError: "",
        selectedDayNumber: dayNumberForSegment(result.itinerary, movedSegmentId) ?? nextDays[0]?.dayNumber ?? 1,
        selectedSegmentId: movedSegmentId,
        selectedRouteOptionId: null,
        previewRouteOptionId: null,
        routeWarnings: result.itinerary.routeWarnings ?? []
      });
      setTripTitle(result.itinerary.title);
      setDays(nextDays);
      setSuccessMessage("已更新游览顺序，路线已重新规划或标记为待重新规划。");
    } catch (error) {
      const message = error instanceof Error ? error.message : "排序保存失败，请稍后重试";
      setDays(previousDays);
      plannerStore.setState({ itineraryPlan: plan, lastPatchError: message });
      setErrorMessage(message);
    } finally {
      setIsSaving(false);
    }
  }

  async function applyTimelinePatch(operations: ItineraryPatchOperation[], contextPatch: Record<string, unknown> = {}) {
    if (readOnly) return;
    if (!plan) {
      setErrorMessage("行程尚未生成，无法保存编辑。");
      return;
    }
    setIsSaving(true);
    setErrorMessage("");
    setSuccessMessage("");
    try {
      const snapshot = plannerStore.getSnapshot();
      const baseVersionId = snapshot.activeVersionId;
      const result = await submitTimelinePatch(plan, snapshot, operations, contextPatch);
      if (!isCurrentVersionedWrite(baseVersionId)) {
        return;
      }
      applyTimelinePatchResult(result, operations, plan, snapshot);
    } catch (error) {
      if (isVersionConflict(error)) {
        await recoverTimelinePatchConflict(operations, contextPatch);
        return;
      }
      const message = error instanceof Error ? error.message : "行程保存失败，请稍后重试";
      plannerStore.setState({ lastPatchError: message });
      setErrorMessage(message);
    } finally {
      setIsSaving(false);
    }
  }

  function submitTimelinePatch(
    targetPlan: ItineraryPlan,
    snapshot: ReturnType<typeof plannerStore.getSnapshot>,
    operations: ItineraryPatchOperation[],
    contextPatch: Record<string, unknown> = {}
  ) {
    return apiClient.patchItinerary(targetPlan.id, {
      sourceType: "manual",
      baseVersionId: snapshot.activeVersionId ?? null,
      preferenceSummary: effectivePreferenceFromSnapshot(snapshot),
      planningContext: buildTimelinePlanningContext(snapshot, targetPlan, {
        selectedSegmentId,
        patchIntent: operations[0]?.op ?? "manual_patch",
        contextPatch
      }),
      operations
    });
  }

  function applyTimelinePatchResult(
    result: Awaited<ReturnType<typeof apiClient.patchItinerary>>,
    operations: ItineraryPatchOperation[],
    beforePlan: ItineraryPlan,
    snapshot: ReturnType<typeof plannerStore.getSnapshot>
  ) {
    const nextDays = createEditableDays(result.itinerary);
    const addedSegmentId = findAddedSegmentId(beforePlan, result.itinerary, operations);
    const removedSegmentId = findRemovedSegmentId(operations);
    const expandedSegmentId = findExpandedSegmentId(operations);
    const replacedSegmentId = findReplacedSegmentId(operations);
    const preferredSelectedSegmentId =
      addedSegmentId ??
      nextSegmentAfterRemoval(beforePlan, result.itinerary, removedSegmentId, selectedSegmentId) ??
      expandedSegmentId ??
      replacedSegmentId ??
      selectedSegmentId ??
      nextDays[0]?.segments[0]?.id ??
      null;
    const selectedAfterPatch = segmentExists(result.itinerary, preferredSelectedSegmentId)
      ? preferredSelectedSegmentId
      : nextDays[0]?.segments[0]?.id ?? null;
    const pendingPoiCandidates = result.pendingPoiCandidates;
    const clearsMapCandidateContext = operations.some((operation) =>
      operation.op === "replace_segment_poi_from_candidate" ||
      operation.op === "replace_segment_poi" ||
      operation.op === "expand_area_poi_candidates" ||
      operation.op === "expand_meal_poi_candidates" ||
      operation.op === "remove_segment"
    );
    plannerStore.setState({
      agentSession: snapshot.agentSession
        ? {
            ...snapshot.agentSession,
            activeVersionId: result.version.id,
            itinerary: result.itinerary,
            pendingPoiCandidates
          }
        : snapshot.agentSession,
      itineraryPlan: result.itinerary,
      activeVersionId: result.version.id,
      pendingPoiCandidates,
      candidateMapPois: clearsMapCandidateContext ? [] : snapshot.candidateMapPois,
      selectedMapPoi: clearsMapCandidateContext ? null : snapshot.selectedMapPoi,
      activeDensityMapComparison: clearsMapCandidateContext ? null : snapshot.activeDensityMapComparison,
      lastPatchError: "",
      selectedDayNumber: dayNumberForSegment(result.itinerary, selectedAfterPatch) ?? nextDays[0]?.dayNumber ?? 1,
      selectedSegmentId: selectedAfterPatch,
      selectedRouteOptionId: null,
      previewRouteOptionId: null,
      routeWarnings: result.itinerary.routeWarnings ?? []
    });
    setTripTitle(result.itinerary.title);
    setDays(nextDays);
    setEditing({ kind: null, value: "" });
    if (removedSegmentId) {
      setSuccessMessage("已删除景点/活动，路线已重新规划或标记为待重新规划。");
    }
  }

  async function recoverTimelinePatchConflict(operations: ItineraryPatchOperation[], contextPatch: Record<string, unknown> = {}) {
    const targetSegmentId = firstSegmentIdFromTimelinePatch(operations) ?? selectedSegmentId;
    try {
      const session = await apiClient.getCurrentAgentSession();
      const latestPlan = session.itinerary;
      applyRefreshedAgentSession(session, targetSegmentId);
      if (!latestPlan || !session.activeVersionId || !canRetryTimelinePatchAfterRefresh(operations, latestPlan)) {
        const message = "行程已更新，请基于最新时间轴重新执行本次 POI 操作。";
        plannerStore.setState({ lastPatchError: "" });
        setSuccessMessage("");
        setErrorMessage(message);
        return;
      }
      const latestSnapshot = plannerStore.getSnapshot();
      const retryResult = await submitTimelinePatch(latestPlan, latestSnapshot, operations, contextPatch);
      if (!isCurrentVersionedWrite(latestSnapshot.activeVersionId)) {
        return;
      }
      applyTimelinePatchResult(retryResult, operations, latestPlan, latestSnapshot);
      setSuccessMessage("已同步最新行程并保存本次 POI 更新。");
      setErrorMessage("");
    } catch (recoveryError) {
      const message = isVersionConflict(recoveryError)
        ? "行程已再次更新，请基于最新时间轴重新执行本次 POI 操作。"
        : routeErrorMessage(recoveryError);
      plannerStore.setState({ lastPatchError: message });
      setSuccessMessage("");
      setErrorMessage(message);
    }
  }

  async function refreshDrivingRoutes(dayId: string, segmentId: string) {
    if (readOnly) return;
    await applyTimelinePatch(
      [{ op: "refresh_routes_for_day", dayId }],
      {
        selectedSegmentId: segmentId,
        preferredRouteMode: "transit",
        includeModes: ["driving", "taxi"],
        routeRefreshReason: "user_requested_driving_taxi_candidates",
        toolRefreshPolicy: { route: "touched_pairs_only" }
      }
    );
    setSuccessMessage("已刷新当前日路线，保留公交/地铁优先，并补充驾车/打车候选。");
  }

  async function refreshVisitFacts() {
    if (!plan || isSaving) return;
    setIsSaving(true);
    setErrorMessage("");
    try {
      const result = await apiClient.refreshItineraryVisitFacts(plan.id);
      const snapshot = plannerStore.getSnapshot();
      plannerStore.setState({
        itineraryPlan: result.plan,
        agentSession: snapshot.agentSession ? { ...snapshot.agentSession, itinerary: result.plan } : null
      });
      setSuccessMessage("已刷新当前行程的开放、预约、门票与放票信息；未知项仍需以官方页面为准。");
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "到访信息刷新失败，行程本身未受影响。");
    } finally {
      setIsSaving(false);
    }
  }

  async function expandConcretePoiCandidates(segment: EditableSegment) {
    if (readOnly) return;
    const isMealExpansion = segment.kind === "meal";
    await applyTimelinePatch([
      {
        op: isMealExpansion ? "expand_meal_poi_candidates" : "expand_area_poi_candidates",
        segmentId: segment.id,
        radius: isMealExpansion ? 2200 : 1500
      }
    ]);
    setSuccessMessage(isMealExpansion ? `已展开「${segment.poiName}」附近的餐饮候选。` : `已展开「${segment.poiName}」附近的具体高德候选。`);
  }

  async function replaceSegmentPoiFromCandidate(segment: EditableSegment, candidate: PendingPoiCandidate, poi: MapPoi) {
    if (readOnly) return;
    await applyTimelinePatch([
      {
        op: "replace_segment_poi_from_candidate",
        segmentId: segment.id,
        candidateId: candidate.id,
        amapPoi: poi,
        notes: poi.sourceNote || `已从高德候选替换为具体地点：${poi.name}`
      }
    ]);
    setSuccessMessage(`已将「${segment.poiName}」替换为「${poi.name}」，路线和票务状态已刷新。`);
  }

  async function useRoute(group: RouteDisplayGroup) {
    if (readOnly) return;
    if (!plan) {
      setErrorMessage("行程尚未生成，无法保存路线。");
      return;
    }
    const route = group.representative;
    setSavingRouteId(route.id);
    setErrorMessage("");
    try {
      const snapshot = plannerStore.getSnapshot();
      const baseVersionId = snapshot.activeVersionId;
      const result = await selectRouteWithVersion(plan, group, snapshot);
      applyRouteSelectionResult(result, route, baseVersionId, "已保存路线选择。");
    } catch (error) {
      if (isVersionConflict(error)) {
        await recoverRouteSelectionConflict(route);
        return;
      }
      const message = routeErrorMessage(error);
      plannerStore.setState({ lastPatchError: message });
      setErrorMessage(message);
    } finally {
      setSavingRouteId(null);
    }
  }

  async function recoverRouteSelectionConflict(route: RouteOption) {
    const routeSnapshot = createRouteSnapshot(route);
    try {
      const session = await apiClient.getCurrentAgentSession();
      const latestPlan = session.itinerary;
      const matchingRoute = latestPlan ? findSafeMatchingRoute(routeSnapshot, latestPlan.routeOptions) : null;
      applyRefreshedAgentSession(session, matchingRoute?.fromSegmentId ?? routeSnapshot.fromSegmentId);
      if (!latestPlan || !matchingRoute) {
        const message = "行程已更新，路线候选已刷新，请基于最新路线重新选择。";
        plannerStore.setState({ lastPatchError: "" });
        setSuccessMessage("");
        setErrorMessage(message);
        return;
      }
      setSavingRouteId(matchingRoute.id);
      const latestSnapshot = plannerStore.getSnapshot();
      const latestBaseVersionId = latestSnapshot.activeVersionId;
      if (!latestBaseVersionId) {
        const message = "行程已更新，路线候选已刷新，请基于最新路线重新选择。";
        plannerStore.setState({ lastPatchError: "" });
        setSuccessMessage("");
        setErrorMessage(message);
        return;
      }
      const retryResult = await selectRouteWithVersion(latestPlan, singleRouteDisplayGroup(matchingRoute), latestSnapshot);
      applyRouteSelectionResult(retryResult, matchingRoute, latestBaseVersionId, "已同步最新行程并保存路线选择。");
    } catch (recoveryError) {
      if (isVersionConflict(recoveryError)) {
        const message = "行程已再次更新，路线候选已刷新，请基于最新路线重新选择。";
        plannerStore.setState({ lastPatchError: "" });
        setSuccessMessage("");
        setErrorMessage(message);
        return;
      }
      const message = routeErrorMessage(recoveryError);
      plannerStore.setState({ lastPatchError: message });
      setErrorMessage(message);
    }
  }

  function selectRouteWithVersion(
    targetPlan: ItineraryPlan,
    routeGroup: RouteDisplayGroup,
    snapshot: ReturnType<typeof plannerStore.getSnapshot>
  ) {
    const route = routeGroup.representative;
    return apiClient.selectRoute(targetPlan.id, route.id, {
      baseVersionId: snapshot.activeVersionId ?? null,
      preferenceSummary: effectivePreferenceFromSnapshot(snapshot),
      planningContext: {
        city: snapshot.selectedCity,
        currentPreferenceSummary: effectivePreferenceFromSnapshot(snapshot),
        itineraryPlanId: targetPlan.id,
        activeVersionId: snapshot.activeVersionId,
        selectedRouteOptionId: route.id,
        selectedSegmentId: route.fromSegmentId ?? selectedSegmentId,
        routeOption: route,
        routeGroup: {
          id: routeGroup.id,
          label: routeGroup.label,
          rawRouteIds: routeGroup.routeIds,
          representativeRouteId: routeGroup.representativeRouteId,
          selectedRawRouteId: routeGroup.selectedRawRouteId,
          selectedRawMode: route.mode || route.transportMode,
          modes: routeGroup.modes
        }
      }
    });
  }

  function applyRouteSelectionResult(
    result: Awaited<ReturnType<typeof apiClient.selectRoute>>,
    route: RouteOption,
    baseVersionId: string | null,
    message: string
  ) {
    if (!isCurrentVersionedWrite(baseVersionId)) {
      return;
    }
    const snapshot = plannerStore.getSnapshot();
    const nextDays = createEditableDays(result.itinerary);
    const selectedAfterRoute = route.fromSegmentId ?? selectedSegmentId;
    plannerStore.setState({
      agentSession: snapshot.agentSession
        ? {
            ...snapshot.agentSession,
            activeVersionId: result.version.id,
            itinerary: result.itinerary,
            pendingPoiCandidates: result.pendingPoiCandidates
          }
        : snapshot.agentSession,
      itineraryPlan: result.itinerary,
      activeVersionId: result.version.id,
      pendingPoiCandidates: result.pendingPoiCandidates,
      selectedDayNumber: dayNumberForSegment(result.itinerary, selectedAfterRoute) ?? snapshot.selectedDayNumber,
      selectedSegmentId: selectedAfterRoute,
      selectedRouteOptionId: route.id,
      previewRouteOptionId: null,
      routeWarnings: result.itinerary.routeWarnings ?? [],
      lastPatchError: "",
      lastPlanningRun: result.planningRun ?? snapshot.lastPlanningRun
    });
    setTripTitle(result.itinerary.title);
    setDays(nextDays);
    setExpandedRouteLegs((current) => {
      const next = new Set(current);
      next.delete(routeLegKey(route));
      return next;
    });
    setCollapsedRouteLegs((current) => new Set(current).add(routeLegKey(route)));
    setErrorMessage("");
    setSuccessMessage(message);
  }

  function applyRefreshedAgentSession(session: AgentSession, preferredSegmentId?: string | null) {
    const latestPlan = session.itinerary;
    const snapshot = plannerStore.getSnapshot();
    const selectedAfterRefresh =
      segmentExists(latestPlan, preferredSegmentId) ? preferredSegmentId ?? null : latestPlan?.days[0]?.segments[0]?.id ?? null;
    const selectedRoute = selectedRouteForSegment(latestPlan, selectedAfterRefresh);
    plannerStore.setState({
      agentSession: session,
      agentSessions: upsertAgentSessionSummary(snapshot.agentSessions, session),
      conversationTurns: session.turns,
      activeVersionId: session.activeVersionId ?? null,
      itineraryPlan: latestPlan,
      pendingPoiCandidates: session.pendingPoiCandidates,
      selectedCity: session.city,
      selectedDayNumber: latestPlan ? dayNumberForSegment(latestPlan, selectedAfterRefresh) ?? latestPlan.days[0]?.dayNumber ?? 1 : 1,
      selectedSegmentId: selectedAfterRefresh,
      selectedRouteOptionId: selectedRoute?.id ?? null,
      previewRouteOptionId: null,
      routeWarnings: latestPlan?.routeWarnings ?? [],
      lastPatchError: ""
    });
    if (latestPlan) {
      setTripTitle(latestPlan.title);
      setDays(createEditableDays(latestPlan));
    }
  }

  async function optimizeRoutes({
    scheduleOnly = false,
    optimizationObjective = "balanced"
  }: { scheduleOnly?: boolean; optimizationObjective?: RouteOptimizationObjective } = {}) {
    if (readOnly) return;
    if (!plan) {
      setErrorMessage("行程尚未生成，无法优化路线。");
      return;
    }
    setIsOptimizingRoutes(true);
    setOptimizeMenuOpen(false);
    setErrorMessage("");
    setSuccessMessage("");
    try {
      const snapshot = plannerStore.getSnapshot();
      const baseVersionId = snapshot.activeVersionId;
      const selectedDayId = selectedSegmentId ? dayIdForSegment(plan, selectedSegmentId) : null;
      const result = await apiClient.optimizeRoutes(plan.id, {
        baseVersionId,
        preferenceSummary: effectivePreferenceFromSnapshot(snapshot),
        dayId: scheduleOnly ? null : selectedDayId,
        optimizationObjective: scheduleOnly ? "balanced" : optimizationObjective,
        planningContext: {
          city: snapshot.selectedCity,
          currentPreferenceSummary: effectivePreferenceFromSnapshot(snapshot),
          itineraryPlanId: plan.id,
          activeVersionId: snapshot.activeVersionId,
          selectedDayNumber: snapshot.selectedDayNumber,
          selectedSegmentId,
          scheduleOnly,
          optimizationObjective: scheduleOnly ? "balanced" : optimizationObjective
        }
      });
      if (!isCurrentVersionedWrite(baseVersionId)) {
        return;
      }
      const nextDays = createEditableDays(result.itinerary);
      const metadata = result.patch.metadata ?? {};
      const routeOptimization = metadata.routeOptimization as { changedCount?: number } | undefined;
      const changedCount = Number(routeOptimization?.changedCount ?? 0);
      const selectedAfterOptimize = segmentExists(result.itinerary, selectedSegmentId)
        ? selectedSegmentId
        : nextDays[0]?.segments[0]?.id ?? null;
      plannerStore.setState({
        agentSession: snapshot.agentSession
          ? {
              ...snapshot.agentSession,
              activeVersionId: result.version.id,
              itinerary: result.itinerary,
              pendingPoiCandidates: result.pendingPoiCandidates
            }
          : snapshot.agentSession,
        itineraryPlan: result.itinerary,
        activeVersionId: result.version.id,
        pendingPoiCandidates: result.pendingPoiCandidates,
        selectedDayNumber: dayNumberForSegment(result.itinerary, selectedAfterOptimize) ?? snapshot.selectedDayNumber,
        selectedSegmentId: selectedAfterOptimize,
        selectedRouteOptionId: selectedRouteForSegment(result.itinerary, selectedAfterOptimize)?.id ?? null,
        previewRouteOptionId: null,
        routeWarnings: result.itinerary.routeWarnings ?? [],
        lastPlanningRun: result.planningRun ?? snapshot.lastPlanningRun,
        lastPatchError: ""
      });
      setTripTitle(result.itinerary.title);
      setDays(nextDays);
      setEditing({ kind: null, value: "" });
      const objectiveLabel =
        optimizationObjective === "fastest" ? "时间最短" : optimizationObjective === "cheapest" ? "费用最少" : "综合最优";
      setSuccessMessage(
        scheduleOnly
          ? "已按当前选中路线重新排时。"
          : changedCount > 0
            ? `已按“${objectiveLabel}”自动切换 ${changedCount} 段路线，并重新排时。`
            : `当前路线已符合“${objectiveLabel}”目标。`
      );
    } catch (error) {
      const message = routeErrorMessage(error);
      plannerStore.setState({ lastPatchError: message });
      setErrorMessage(message);
    } finally {
      setIsOptimizingRoutes(false);
    }
  }

  function previewRoute(group: RouteDisplayGroup) {
    plannerStore.setState({
      previewRouteOptionId: group.representativeRouteId
    });
  }

  async function removeSegment(segmentId: string, poiName: string) {
    if (readOnly) return;
    if (!window.confirm(`确认删除 ${poiName} 吗？相关路线会重新规划或标记为待重新规划。`)) {
      return;
    }
    await applyTimelinePatch([{ op: "remove_segment", segmentId }]);
  }

  function toggleRouteLeg(route: RouteOption, isExpanded: boolean) {
    const key = routeLegKey(route);
    setExpandedRouteLegs((current) => {
      const next = new Set(current);
      if (isExpanded) {
        next.delete(key);
      } else {
        next.add(key);
      }
      return next;
    });
    setCollapsedRouteLegs((current) => {
      const next = new Set(current);
      if (isExpanded) {
        next.add(key);
      } else {
        next.delete(key);
      }
      return next;
    });
  }

  return (
    <section className="timeline-panel" aria-label="Daily itinerary timeline content">
      <div className="timeline-heading">
        <div className="trip-title-block">
          {editing.kind === "trip" ? (
            <div className="inline-editor">
              <input
                aria-label="旅行标题输入"
                value={editing.value}
                onChange={(event) => setEditing((current) => ({ ...current, value: event.target.value }))}
              />
              <button type="button" onClick={confirmTripTitle}>确认</button>
              <button type="button" onClick={cancelEdit}>取消</button>
            </div>
          ) : readOnly ? (
            <h2>{tripTitle}</h2>
          ) : (
            <button className="editable-heading" type="button" onClick={beginTripTitleEdit}>
              {tripTitle}
            </button>
          )}
        </div>
        {!readOnly ? <div className="timeline-actions">
          <div className="route-optimize-menu">
            <button
              aria-expanded={optimizeMenuOpen}
              aria-haspopup="menu"
              disabled={isOptimizingRoutes || isSaving}
              onClick={() => setOptimizeMenuOpen((open) => !open)}
              type="button"
            >
              {isOptimizingRoutes ? "优化中" : "优化路线"}
            </button>
            {optimizeMenuOpen ? (
              <div className="route-optimize-options" role="menu" aria-label="路线优化目标">
                <button role="menuitem" type="button" onClick={() => optimizeRoutes({ optimizationObjective: "balanced" })}>
                  综合最优
                </button>
                <button role="menuitem" type="button" onClick={() => optimizeRoutes({ optimizationObjective: "fastest" })}>
                  时间最短
                </button>
                <button role="menuitem" type="button" onClick={() => optimizeRoutes({ optimizationObjective: "cheapest" })}>
                  费用最少
                </button>
              </div>
            ) : null}
          </div>
          <button disabled={isOptimizingRoutes || isSaving} onClick={() => optimizeRoutes({ scheduleOnly: true })} type="button">
            {isOptimizingRoutes ? "排期中" : "自动排期"}
          </button>
        </div> : null}
      </div>

      {errorMessage ? <p className="timeline-error" role="alert">{errorMessage}</p> : null}
      {successMessage ? <p className="timeline-success" role="status">{successMessage}</p> : null}
      {isSaving ? <p className="timeline-saving" role="status">正在保存行程修改...</p> : null}
      {routeWarningSummary.hasWarnings ? (
        <div className="route-warning-list" role="alert">
          <p>{routeWarningSummary.summary}</p>
          <details>
            <summary>开发者详情</summary>
            {routeWarningSummary.rawWarnings.map((warning) => <p key={warning}>{warning}</p>)}
          </details>
        </div>
      ) : null}
      {plan ? <RiskSignals plan={plan} /> : <FallbackRiskSignals />}

      <div className="day-workspace-list">
        {days.map((day) => {
          const dayTotals = calculateDayTotals(day);
          return (
            <section className="day-card" key={day.id} aria-label={`Day ${day.dayNumber} itinerary group`}>
              <header className="day-card-header">
                <div>
                  <span className="day-label">Day {day.dayNumber}</span>
                  {editing.kind === "day" && editing.id === day.id ? (
                    <div className="inline-editor">
                      <input
                        aria-label={`Day ${day.dayNumber} 标题输入`}
                        value={editing.value}
                        onChange={(event) => setEditing((current) => ({ ...current, value: event.target.value }))}
                      />
                      <button type="button" onClick={() => confirmDayTitle(day.id)}>确认</button>
                      <button type="button" onClick={cancelEdit}>取消</button>
                    </div>
                  ) : readOnly ? (
                    <strong>{day.dayTitle}</strong>
                  ) : (
                    <button className="day-title-button" type="button" onClick={() => beginDayTitleEdit(day)}>
                      {day.dayTitle}
                    </button>
                  )}
                </div>
                <button type="button" onClick={() => toggleDay(day.id)}>
                  {day.collapsed ? "展开" : "折叠"}
                </button>
              </header>
              <div className="day-total-row">
                <span>{routeCoverageLabel(dayTotals, dayTotals.walkingDistanceMeters)}</span>
                <span>活动/用餐停留 {formatDuration(dayTotals.activityDurationMinutes ?? dayTotals.durationMinutes)}</span>
                <span>已知交通 {formatDuration(dayTotals.travelDurationMinutes ?? 0)}</span>
                <span>显式缓冲 {formatDuration(dayTotals.explicitBufferMinutes ?? dayTotals.bufferDurationMinutes ?? 0)}</span>
                <span>真实空档 {formatDuration(dayTotals.unallocatedGapMinutes ?? 0)}</span>
                <span>日程跨度 {formatDuration(dayTotals.scheduleSpanMinutes ?? dayTotals.durationMinutes)}</span>
                {!plan.budgetBreakdown ? <span>花费 ¥{Math.round(dayTotals.estimatedCost)}</span> : null}
              </div>

              {day.collapsed ? null : (
                <div className="day-segments-scroll" aria-label={`Day ${day.dayNumber} 当日时间轴`}>
                  <ol className="segment-list">
                    {day.pendingSlots.map((slot) => (
                      <li
                        aria-label={`Day ${day.dayNumber} 待补时段 ${pendingSlotTimeLabel(slot)}`}
                        className="timeline-pending-slot"
                        data-testid="timeline-pending-slot"
                        key={slot.id}
                        style={{ order: pendingSlotTimelineOrder(slot, day.segments) }}
                      >
                        <button
                          aria-label={`打开 Day ${day.dayNumber} ${pendingSlotTimeLabel(slot)} 待补地点候选`}
                          className="timeline-pending-slot-action"
                          disabled={!onSelectPendingSlot}
                          onClick={() => onSelectPendingSlot?.(slot)}
                          type="button"
                        >
                          <span className="timeline-pending-time">
                            {pendingSlotTimeLabel(slot)}
                          </span>
                          <span className="timeline-pending-main">
                            <strong>{slot.label || `待补：${slot.rawNeed || "行程"}`}</strong>
                            {slot.constraintSummary ? <small>{slot.constraintSummary}</small> : null}
                            {slot.timingStatus === "awaiting_route_confirmation" ? (
                              <small>当前位置按已确认日程与时间约束预留，交通时间待路线确认。</small>
                            ) : null}
                            {slot.timingStatus === "schedule_conflict_pending" ? (
                              <small>当前日程暂无无冲突空档，确认地点后将重排当天时间。</small>
                            ) : null}
                            <small>点击可在地图对比该槽位候选；最终地点仍在对话栏确认。</small>
                          </span>
                        </button>
                      </li>
                    ))}
                    {day.segments.map((segment, segmentIndex) => {
                      const ticket = ticketBySegment.get(segment.id);
                      const routeCandidates = routesForSegment(plan, day.id, segment.id);
                      const selectedRoute = selectedRouteForCandidates(routeCandidates);
                      const hasNextRouteAnchor = isRouteAnchorSegment(segment) && Boolean(nextRouteAnchorSegment(day, segmentIndex));
                      const routeColor = selectedRoute ? routeLegColor(selectedRoute, routeColorMap) : undefined;
                      const routeExpanded =
                        Boolean(selectedRoute) &&
                        (expandedRouteLegs.has(routeLegKey(selectedRoute)) ||
                          (segment.id === selectedSegmentId && !collapsedRouteLegs.has(routeLegKey(selectedRoute))));
                      return (
                        <li
                          aria-label={`拖放到 ${segment.poiName}`}
                          data-route-track-layer="behind-content"
                          className={`segment-item ${segment.id === selectedSegmentId ? "selected" : ""} ${
                            segment.id === draggedSegmentId ? "dragging" : ""
                          } ${segment.id === dragDropTarget?.segmentId && segment.id !== draggedSegmentId ? `drop-target drop-${dragDropTarget.position}` : ""}`}
                          key={segment.id}
                          onDragLeave={() =>
                            setDragDropTarget((current) => (current?.segmentId === segment.id ? null : current))
                          }
                          onDragOver={(event) => dragOverSegment(segment.id, event)}
                          onDrop={(event) => dropSegment(day, segment.id, event)}
                          style={{
                            order: timelineOrder(segment.startTime),
                            ...(routeColor ? { "--route-color": routeColor } : {})
                          } as CSSProperties}
                        >
                          {segment.id === dragDropTarget?.segmentId && segment.id !== draggedSegmentId ? (
                            <span className="segment-drop-indicator" aria-hidden="true">
                              {dragDropTarget.position === "before" ? "放到此项上方" : "放到此项下方"}
                            </span>
                          ) : null}
                          {!readOnly ? <button
                            aria-label={`拖动 ${segment.poiName}`}
                            className="drag-segment-handle"
                            draggable
                            onDragEnd={() => {
                              setDraggedSegmentId(null);
                              setDragDropTarget(null);
                            }}
                            onDragStart={(event) => beginSegmentDrag(segment.id, event)}
                            title="拖动调整同一天内的游览顺序"
                            type="button"
                          >
                            ::
                          </button> : null}
                          {editing.kind === "time" && editing.id === segment.id ? (
                            <div className="time-editor-popover">
                              <label htmlFor={`time-${segment.id}`}>到达时间</label>
                              <input
                                id={`time-${segment.id}`}
                                aria-label={`${segment.poiName} 时间输入`}
                                type="time"
                                step={300}
                                value={editing.value}
                                onChange={(event) => setEditing((current) => ({ ...current, value: event.target.value }))}
                              />
                              <button type="button" onClick={() => confirmTimeEdit(day.id, segment.id)}>确认</button>
                              <button type="button" onClick={cancelEdit}>取消</button>
                            </div>
                          ) : readOnly ? (
                            <span className="segment-time-pill" style={TIMELINE_TIME_PILL_LAYOUT_STYLE}><span className="time-label">到达</span><strong>{segment.startTime}</strong></span>
                          ) : (
                            <button
                              aria-label={`编辑 ${segment.poiName} 到达时间`}
                              className="segment-time-pill"
                              style={TIMELINE_TIME_PILL_LAYOUT_STYLE}
                              type="button"
                              onClick={() => beginTimeEdit(segment.id, segment.startTime)}
                            >
                              <span className="time-label">到达</span>
                              <strong>{segment.startTime}</strong>
                              <span className="edit-hint">编辑</span>
                            </button>
                          )}
                          <button className="segment-main" type="button" onClick={() => onSelectSegment(segment.id)}>
                            <span data-testid="timeline-segment-name">{segment.poiName}</span>
                            <small>{segment.agentNotes}</small>
                            {groundingStatusLabel(segment) ? (
                              <small className="poi-grounding-warning">{groundingStatusLabel(segment)}</small>
                            ) : null}
                          </button>
                          {!readOnly && segmentNeedsConcretePoi(segment) ? (
                            <ConcretePoiCandidatePanel
                              candidates={pendingPoiCandidatesForSegment(pendingPoiCandidates, segment.id, segment.poiName)}
                              disabled={isSaving}
                              onExpand={() => expandConcretePoiCandidates(segment)}
                              onReplace={(candidate, poi) => replaceSegmentPoiFromCandidate(segment, candidate, poi)}
                              segment={segment}
                            />
                          ) : null}
                          <div className="segment-facts">
                            <span>{durationRangeLabel(segment)}</span>
                            <span>{segmentCostLabel(segment)}</span>
                            <span>{ticket ? ticketStatusLabel(ticket) : "预约未查询"}</span>
                          </div>
                          <SegmentVisitFactsPanel
                            disabled={isSaving}
                            facts={plan.visitFactsBySegment?.[segment.id]}
                            onRefresh={() => void refreshVisitFacts()}
                          />
                          {ticket ? <SegmentTicketSource ticket={ticket} /> : null}
                          {!readOnly ? <button
                            aria-label={`删除 ${segment.poiName}`}
                            className="delete-segment-button"
                            onClick={() => removeSegment(segment.id, segment.poiName)}
                            type="button"
                          >
                            删除
                          </button> : null}
                          {hasNextRouteAnchor && selectedRoute ? (
                            <RouteLegSummary
                              expanded={routeExpanded}
                              onToggle={() => toggleRouteLeg(selectedRoute, routeExpanded)}
                              route={selectedRoute}
                              routeColorMap={routeColorMap}
                            />
                          ) : null}
                          {!readOnly && hasNextRouteAnchor && ((selectedRoute && routeExpanded) || (!selectedRoute && segment.id === selectedSegmentId)) ? (
                            <RouteCandidatePanel
                              candidates={routeCandidates}
                              hasNextSegment={hasNextRouteAnchor}
                              isRefreshing={isSaving}
                              onPreview={previewRoute}
                              onRefreshDriving={() => refreshDrivingRoutes(day.id, segment.id)}
                              onUse={useRoute}
                              routeColorMap={routeColorMap}
                              savingRouteId={savingRouteId}
                            />
                          ) : null}
                        </li>
                      );
                    })}
                  </ol>
                  {!readOnly ? <button className="add-segment-button" type="button" onClick={() => addSegment(day.id)}>
                    + 添加景点/活动
                  </button> : null}
                </div>
              )}
            </section>
          );
        })}
      </div>

      {!readOnly ? <button className="add-day-button" type="button" onClick={addDay}>
        + 增加日程安排
      </button> : null}

      <div className="cost-summary">
        <header><span>行程总计</span><strong>当前可估（暂估） ¥{Math.round(budgetSummary?.provisionalPreferred ?? tripTotals.estimatedCost)}</strong></header>
        <section aria-label="预算统计" className="cost-summary-group">
          <b>预算</b>
          {budgetSummary ? <span>已知合计 ¥{Math.round(budgetSummary.knownTotal)}</span> : null}
          {budgetSummary ? <span>暂估范围 ¥{Math.round(budgetSummary.provisionalMin)}–¥{Math.round(budgetSummary.provisionalMax)}</span> : null}
          {budgetSummary?.unknownItems.map((item) => <span className="cost-summary-pending" key={item}>待核验：{item}</span>)}
        </section>
        <section aria-label="时间统计" className="cost-summary-group">
          <b>时间</b>
          <span>活动/用餐停留 {formatDuration(tripTotals.activityDurationMinutes ?? tripTotals.durationMinutes)}</span>
          <span>已知交通 {formatDuration(tripTotals.travelDurationMinutes ?? 0)}</span>
          <span>显式缓冲 {formatDuration(tripTotals.explicitBufferMinutes ?? tripTotals.bufferDurationMinutes ?? 0)}</span>
          <span>真实空档 {formatDuration(tripTotals.unallocatedGapMinutes ?? 0)}</span>
          <span>日程跨度（各日合计） {formatDuration(tripTotals.scheduleSpanMinutes ?? tripTotals.durationMinutes)}</span>
        </section>
        <section aria-label="路线统计" className="cost-summary-group">
          <b>路线</b><span>{routeCoverageLabel(tripTotals, tripTotals.walkingDistanceMeters)}</span>
        </section>
        <small>统计来自当前右侧时间栏，Agent 对话会自动携带最新上下文。</small>
      </div>
    </section>
  );
}

export function formatTimelineForCopy(plan: ItineraryPlan, tripTitle: string, days: EditableDay[]) {
  const totals = calculateTripTotals(days);
  const budget = authoritativeBudgetSummary(plan, totals.estimatedCost);
  const lines = [
    `时间轴：${tripTitle || plan.title}`,
    `城市：${plan.city}`,
    `状态：${plan.status}`,
    `预算档位：${budgetTierCopyLabel(budget.tier)}`,
    `金额上限：${budget.numericTarget === null ? "未指定" : `¥${Math.round(budget.numericTarget)}`}`,
    `已知合计：¥${Math.round(budget.knownTotal)}`,
    `当前可估（暂估）：¥${Math.round(budget.provisionalPreferred)}`,
    `暂估范围：¥${Math.round(budget.provisionalMin)}–¥${Math.round(budget.provisionalMax)}`,
    `未知项：${budget.unknownItems.length ? budget.unknownItems.join("；") : "无"}`,
    `活动/用餐停留：${formatDuration(totals.activityDurationMinutes ?? totals.durationMinutes)}`,
    `已知交通：${formatDuration(totals.travelDurationMinutes ?? 0)}`,
    `显式缓冲：${formatDuration(totals.explicitBufferMinutes ?? totals.bufferDurationMinutes ?? 0)}`,
    `真实空档：${formatDuration(totals.unallocatedGapMinutes ?? 0)}${(totals.unknownRouteLegCount ?? 0) > 0 ? `；待补路线 ${totals.unknownRouteLegCount} 段` : ""}`,
    `日程跨度（各日合计）：${formatDuration(totals.scheduleSpanMinutes ?? totals.durationMinutes)}`,
    `路线覆盖：${totals.coveredRouteLegCount ?? 0}/${totals.requiredRouteLegCount ?? 0}`,
    `已知路线距离：${formatDistance(totals.walkingDistanceMeters)}`,
    (totals.unknownRouteLegCount ?? 0) > 0 ? "交通费用：部分估算" : "交通费用：已覆盖全部路线段"
  ];
  for (const day of days) {
    const dayTotals = calculateDayTotals(day);
    lines.push(
      "",
      `Day ${day.dayNumber}：${day.dayTitle}`,
      `小计：活动/用餐停留 ${formatDuration(dayTotals.activityDurationMinutes ?? dayTotals.durationMinutes)} · 已知交通 ${formatDuration(dayTotals.travelDurationMinutes ?? 0)} · 显式缓冲 ${formatDuration(dayTotals.explicitBufferMinutes ?? dayTotals.bufferDurationMinutes ?? 0)} · 真实空档 ${formatDuration(dayTotals.unallocatedGapMinutes ?? 0)} · 日程跨度 ${formatDuration(dayTotals.scheduleSpanMinutes ?? dayTotals.durationMinutes)} · 路线覆盖 ${dayTotals.coveredRouteLegCount ?? 0}/${dayTotals.requiredRouteLegCount ?? 0} · ${formatDistance(dayTotals.walkingDistanceMeters)}${plan.budgetBreakdown ? "" : ` · ¥${Math.round(dayTotals.estimatedCost)}`}`
    );
    for (const segment of day.segments) {
      const route = selectedRouteForCandidates(routesForSegment(plan, day.id, segment.id));
      lines.push(
        `- ${segment.startTime} ${segment.poiName}`,
        `  类型：${segment.kind}`,
        `  停留：${durationRangeLabel(segment)}`,
        `  费用：${segmentCostLabel(segment)}`,
        `  备注：${segment.agentNotes || "无"}`
      );
      if (segment.groundingStatus) {
        lines.push(`  地图状态：${groundingStatusLabel(segment) || segment.groundingStatus}`);
      }
      if (route) {
        lines.push(
          `  下一段路线：${routeDisplayLabel(route)} · ${formatDuration(route.durationMinutes)} · ${formatDistance(route.distanceMeters)} · ${routeCostLabel(route)}`
        );
      }
    }
  }
  if (plan.routeWarnings?.length) {
    lines.push("", "路线警告：", ...plan.routeWarnings.map((warning) => `- ${userRouteWarning(plan, warning)}`));
  }
  lines.push(...formatRiskSignalsForCopy(plan, false));
  return lines.join("\n");
}

export function formatTimelineDebugForCopy(plan: ItineraryPlan, tripTitle: string, days: EditableDay[]) {
  return [formatTimelineForCopy(plan, tripTitle, days), ...formatRiskDebugForCopy(plan)].join("\n");
}

function formatRiskSignalsForCopy(plan: ItineraryPlan, includeDebug: boolean) {
  const weather = plan.weatherSignals[0];
  const traffic = plan.trafficCrowdingSignals[0];
  const poiRiskAlerts = plan.poiRiskAlerts ?? [];
  const weatherSummary = weather ? `${weather.date ? `${formatCopyWeatherDate(weather.date)} · ` : ""}${weather.dailySummary}` : "待 Agent 查询";
  const riskSummary = traffic ? copyTrafficRiskSummary(traffic) : "待 Agent 查询";
  const lines = ["", "风险提示：", `- 概览：天气：${weatherSummary} · 拥挤：${riskSummary}`];

  lines.push("天气：");
  if (weather) {
    lines.push(`- 摘要：${weather.dailySummary}`);
    lines.push(`- 影响：${weather.purposeImpactReason || "暂无天气影响说明"}`);
    lines.push(
      `- 来源：${copySourceLabel(weather.source || weather.providerName, "天气服务")}` +
        `${weather.date ? ` · 天气日期 ${formatCopyWeatherDate(weather.date)}` : ""}` +
        `${weather.queriedAt ? ` · 查询 ${formatQueryTime(weather.queriedAt)}` : ""}`
    );
    lines.push(
      `- 状态：${copyStatusLabel(weather.dataStatus)} · 风险等级：${copyWeatherRiskLevelLabel(weather)}` +
        `${typeof weather.confidence === "number" ? ` · 置信度 ${Math.round(weather.confidence * 100)}%` : ""}` +
        `${weather.fallbackUsed && weather.dataStatus !== "fallback" ? " · 已降级" : ""}`
    );
    if (weather.fallbackUsed) {
      lines.push(`- 天气查询已降级：${friendlyTimelineMessage(weather.failureReason || "天气服务暂时不可用，请稍后重新查询。")}`);
    }
    if (weather.failureReason && !weather.fallbackUsed) {
      lines.push(`- 天气查询失败：${friendlyTimelineMessage(weather.failureReason)}`);
    }
    if (weather.userVisibleCaveat) {
      lines.push(`- ${friendlyTimelineMessage(weather.userVisibleCaveat)}`);
    }
  } else {
    lines.push("- 待 Agent 在识别到具体旅行日期后联网查询。");
  }

  lines.push("拥挤：");
  if (traffic) {
    lines.push(`- 摘要：${copyTrafficRiskSummary(traffic)}`);
    if (traffic.estimatedReason) {
      lines.push(`- 依据：${traffic.estimatedReason}`);
    }
    lines.push(
      `- 来源：${copySourceLabel(traffic.source, "交通服务")}` +
        `${traffic.queriedAt ? ` · 查询 ${formatQueryTime(traffic.queriedAt)}` : ""}`
    );
  } else {
    lines.push("- 待 Agent 查询。");
  }

  if (weather?.source?.includes("mock") || traffic?.source?.includes("mock")) {
    lines.push("- 真实天气/风险数据待接入，当前结果不作为完整风险判断。");
  }

  lines.push("景点风险搜索：");
  if (poiRiskAlerts.length) {
    for (const alert of poiRiskAlerts) {
      const visibleSources = alert.sources.filter(
        (source) => !source.type && String(source.credibilityRank || "").toLowerCase() === "official" && (source.title || source.url)
      );
      lines.push(`- ${alert.poiName}：${copyPoiRiskStatusLabel(alert)}`);
      lines.push(`  摘要：${alert.summary}`);
      if (alert.failureReason) {
        lines.push(`  ${alert.status === "degraded" ? "风险判断提示" : "搜索提示"}：${friendlyRiskSearchMessage(alert.failureReason)}`);
      }
      if (alert.userVisibleCaveat) {
        lines.push(`  ${friendlyRiskSearchMessage(alert.userVisibleCaveat)}`);
      }
      if (visibleSources.length) {
        lines.push(`  来源链接：`);
        for (const source of visibleSources) {
          lines.push(`  - ${source.title ?? source.url ?? "未命名来源"}${source.url ? `：${source.url}` : ""}`);
        }
      }
      if (includeDebug) {
        const debugLines = formatPoiRiskDebugForCopy(alert);
        if (debugLines.length) {
          lines.push("  Debug 详情：", ...debugLines.map((line) => `  ${line}`));
        }
      }
    }
  } else {
    lines.push("- 待 Agent 联网搜索近期景点风险。");
  }

  return lines;
}

function formatRiskDebugForCopy(plan: ItineraryPlan) {
  const lines = ["", "Debug 详情："];
  let count = 0;
  for (const alert of plan.poiRiskAlerts ?? []) {
    const debugLines = formatPoiRiskDebugForCopy(alert);
    if (!debugLines.length) {
      continue;
    }
    count += debugLines.length;
    lines.push(`- ${alert.poiName}`, ...debugLines.map((line) => `  ${line}`));
  }
  return count ? lines : [];
}

function ConcretePoiCandidatePanel({
  candidates,
  disabled,
  onExpand,
  onReplace,
  segment
}: {
  candidates: PendingPoiCandidate[];
  disabled: boolean;
  onExpand: () => void;
  onReplace: (candidate: PendingPoiCandidate, poi: MapPoi) => void;
  segment: EditableSegment;
}) {
  return (
    <div className="concrete-poi-candidates" aria-label={`${segment.poiName} 具体地点候选`}>
      <div className="concrete-poi-candidate-header">
        <span>{concretePoiActionLabel(segment)}</span>
        <button disabled={disabled} onClick={onExpand} type="button">
          {concretePoiExpandButtonLabel(segment)}
        </button>
      </div>
      {candidates.length ? (
        <div className="concrete-poi-candidate-list">
          {candidates.flatMap((candidate) =>
            candidate.candidates.slice(0, 4).map((poi) => (
              <button
                className="concrete-poi-candidate"
                disabled={disabled}
                key={`${candidate.id}:${poi.id}`}
                onClick={() => onReplace(candidate, poi)}
                type="button"
              >
                <strong>{poi.name}</strong>
                <small>{poi.type || "高德 POI"}</small>
                <span>{poi.address || poi.district || "来源：高德地图"}</span>
              </button>
            ))
          )}
        </div>
      ) : (
        <small>先展开高德附近候选，再选择具体场馆/入口/地点。</small>
      )}
    </div>
  );
}

function SegmentVisitFactsPanel({
  disabled,
  facts,
  onRefresh
}: {
  disabled: boolean;
  facts?: NonNullable<ItineraryPlan["visitFactsBySegment"]>[string];
  onRefresh: () => void;
}) {
  const rows = [
    ["开放时间", facts?.facts.openingHours],
    ["预约要求", facts?.facts.reservation],
    ["门票价格", facts?.facts.ticketPrice],
    ["放票时间", facts?.facts.ticketRelease]
  ] as const;
  return (
    <details className="segment-visit-facts" style={VISIT_FACTS_LAYOUT_STYLE}>
      <summary>到访信息{facts ? ` · ${facts.refreshStatus}` : " · 待查询"}</summary>
      <dl>
        {rows.map(([label, item]) => (
          <div key={label}>
            <dt>{label}</dt>
            <dd>
              <span data-status={item?.status ?? "unknown"}>{item?.valueText ?? "待核验"}</span>
              {item?.caveat ? <small>{item.caveat}</small> : null}
              {(item?.sourceRefs ?? []).slice(0, 2).map((source) =>
                source.url ? (
                  <a href={source.url} key={source.url} rel="noreferrer" target="_blank">
                    {source.sourceName || source.title || "查看来源"}
                  </a>
                ) : null
              )}
            </dd>
          </div>
        ))}
      </dl>
      {facts ? <small>适用日期：{facts.visitDate} · 查询于 {new Date(facts.queriedAt).toLocaleString("zh-CN")}</small> : null}
      <button disabled={disabled} onClick={onRefresh} type="button">刷新到访信息</button>
    </details>
  );
}

function SegmentTicketSource({ ticket }: { ticket: NonNullable<ItineraryPlan["ticketLookupResults"][number]> }) {
  const displaySource = ticketDisplaySource(ticket);
  const officialEntryAvailable = hasOfficialReservationEntry(ticket);
  return (
    <div className="segment-ticket-source">
      <details>
        <summary>
          <span>预约</span>
          <small>{ticketStatusLabel(ticket)}</small>
        </summary>
        <p>来源：{displaySource}</p>
        <p>查询 {formatQueryTime(ticket.queriedAt)} · 置信度 {Math.round(ticket.confidence * 100)}%</p>
        {ticket.providerFailureReason ? <p className="risk-data-caveat">查询失败：{friendlyTimelineMessage(ticket.providerFailureReason)}</p> : null}
        {ticket.caveat ? <p>{friendlyTimelineMessage(ticket.caveat)}</p> : null}
      </details>
      {officialEntryAvailable ? (
        <a
          aria-label={`打开${displaySource}预约来源`}
          className="ticket-source-link"
          href={ticket.sourceUrl}
          rel="noreferrer"
          target="_blank"
          title={`打开${displaySource}预约来源`}
        >
          查看官方预约入口
        </a>
      ) : (
        <span className="ticket-source-placeholder">未找到官方入口</span>
      )}
    </div>
  );
}

function ticketStatusLabel(ticket: NonNullable<ItineraryPlan["ticketLookupResults"][number]>) {
  if (ticket.providerFailureReason || (!ticket.sourceName && !ticket.sourceUrl)) {
    return "查询失败/待确认";
  }
  if (["available", "reservation_required"].includes(ticket.status) && !hasOfficialReservationEntry(ticket)) {
    return "未找到官方入口";
  }
  return reservationStatusLabel(ticket.status);
}

function reservationStatusLabel(status: string | undefined) {
  const labels: Record<string, string> = {
    reservation_required: "需预约",
    available: "官方入口已找到",
    needs_concrete_poi: "请选择具体地点",
    not_checked: "预约未查询",
    open: "无需预约",
    estimated: "待确认",
    unknown: "待确认",
    unavailable: "未找到官方入口",
    closed: "暂停开放"
  };
  return labels[status ?? ""] ?? status ?? "待确认";
}

function hasOfficialReservationEntry(ticket: NonNullable<ItineraryPlan["ticketLookupResults"][number]>) {
  return Boolean(ticket.sourceUrl && String(ticket.credibilityRank || "").toLowerCase() === "official");
}

function riskLevelLabel(level: string | undefined) {
  const labels: Record<string, string> = {
    high: "高风险",
    medium: "中风险",
    low: "低风险",
    risky: "有风险",
    ideal: "适宜",
    neutral: "影响较低",
    unknown: "待判断",
    unavailable: "不可用",
    bad_weather: "天气风险"
  };
  return labels[level ?? ""] ?? level ?? "";
}

function routeRiskLabel(route: RouteOption) {
  if (isLowLikeRisk(route.crowdingRisk) && !hasReliableRouteRisk(route)) {
    return "风险待核验";
  }
  return riskLevelLabel(route.crowdingRisk) || "待判断";
}

function isLowLikeRisk(level: string | undefined) {
  return ["low", "ideal", "neutral"].includes(String(level || "").toLowerCase());
}

function hasReliableRouteRisk(route: RouteOption) {
  const source = `${route.source || ""} ${route.provider || ""}`.toLowerCase();
  const status = String(route.routeStatus || "").toLowerCase();
  if (route.error || ["waiting_for_poi_grounding", "provider_rate_limited", "route_unavailable"].includes(status)) {
    return false;
  }
  return !/(mock|unavailable|not configured|provider unavailable)/i.test(source);
}

function ticketDisplaySource(ticket: NonNullable<ItineraryPlan["ticketLookupResults"][number]>) {
  if (ticket.sourceName && !isUrlLike(ticket.sourceName)) {
    return ticket.sourceName;
  }
  const sourceUrlLike = ticket.sourceUrl || ticket.sourceName;
  if (sourceUrlLike && isUrlLike(sourceUrlLike)) {
    return hostnameFromUrlLike(sourceUrlLike);
  }
  return ticket.sourceName || "未返回可用来源";
}

function hostnameFromUrlLike(value: string) {
  try {
    const normalized = /^https?:\/\//i.test(value.trim()) ? value.trim() : `https://${value.trim()}`;
    return new URL(normalized).hostname.replace(/^www\./, "") || "外部来源";
  } catch {
    return "外部来源";
  }
}

function isUrlLike(value: string) {
  const normalized = value.trim();
  return /^(https?:\/\/|www\.)/i.test(normalized) || /^[a-z0-9-]+(\.[a-z0-9-]+)+(\/|\?|#|$)/i.test(normalized);
}

function routesForSegment(plan: ItineraryPlan | null, dayId: string, segmentId: string) {
  if (!plan) {
    return [];
  }
  const day = plan.days.find((item) => item.id === dayId);
  const segmentIndex = day?.segments.findIndex((segment) => segment.id === segmentId) ?? -1;
  const current = day?.segments[segmentIndex];
  const next = day ? nextRouteAnchorSegment(day, segmentIndex) : undefined;
  if (!current || !isRouteAnchorSegment(current) || !next) {
    return [];
  }
  const directCandidates = plan.routeOptions.filter((route) => route.fromSegmentId === current.id && route.toSegmentId === next.id);
  return sortRouteCandidates(directCandidates.filter(isDisplayableRoute));
}

function dayHasPendingRoute(plan: ItineraryPlan | null, day: EditableDay) {
  const routeAnchors = routeAnchorSegments(day);
  if (!plan || routeAnchors.length < 2) {
    return false;
  }
  return routeAnchors.slice(0, -1).some((segment) => routesForSegment(plan, day.id, segment.id).length === 0);
}

function tripHasPendingRoute(plan: ItineraryPlan | null, days: EditableDay[]) {
  return days.some((day) => dayHasPendingRoute(plan, day));
}

function routeCoverageLabel(
  totals: ReturnType<typeof calculateDayTotals>,
  distanceMeters: number
) {
  const required = totals.requiredRouteLegCount ?? 0;
  const covered = totals.coveredRouteLegCount ?? 0;
  const known = covered > 0 ? `已知路线 ${formatDistance(distanceMeters)} · ` : "路线覆盖 ";
  return `${known}${covered}/${required} 路段已生成${covered < required ? ` · ${required - covered} 段待补` : ""}`;
}

function budgetTierCopyLabel(tier: ItineraryPlan["budgetTier"]) {
  return ({ low: "低预算", medium: "中等预算", high: "较高预算" } as Record<string, string>)[tier ?? ""] ?? "预算档位待确认";
}

function userRouteWarning(plan: ItineraryPlan, warning: string) {
  const namesById = new Map(plan.days.flatMap((day) => day.segments).map((segment) => [segment.id, segment.poi.name]));
  return warning.replace(/seg_[a-zA-Z0-9_]+/g, (segmentId) => namesById.get(segmentId) ?? "相关地点");
}

function createRouteSnapshot(route: RouteOption): RouteSelectionSnapshot {
  return {
    id: route.id,
    fromSegmentId: route.fromSegmentId,
    toSegmentId: route.toSegmentId,
    fromPoiId: route.fromPoiId,
    toPoiId: route.toPoiId,
    mode: route.mode,
    transportMode: route.transportMode
  };
}

function findSafeMatchingRoute(snapshot: RouteSelectionSnapshot, routes: RouteOption[]) {
  return (
    routes.find((route) => route.id === snapshot.id && sameSegmentLeg(snapshot, route)) ??
    routes.find((route) => sameSegmentLeg(snapshot, route) && sameRouteMode(snapshot, route)) ??
    null
  );
}

function sameSegmentLeg(snapshot: RouteSelectionSnapshot, route: RouteOption) {
  return route.fromSegmentId === snapshot.fromSegmentId && route.toSegmentId === snapshot.toSegmentId;
}

function sameRouteMode(snapshot: RouteSelectionSnapshot, route: RouteOption) {
  return [route.mode, route.transportMode].some((value) => value === snapshot.mode || value === snapshot.transportMode);
}

function selectedRouteForSegment(plan: ItineraryPlan | null, segmentId: string | null) {
  if (!plan || !segmentId) {
    return null;
  }
  const day = plan.days.find((item) => item.segments.some((segment) => segment.id === segmentId));
  if (!day) {
    return null;
  }
  return selectedRouteForCandidates(routesForSegment(plan, day.id, segmentId));
}

function routeAnchorSegments<T extends { kind: string }>(day: { segments: T[] }) {
  return day.segments.filter(isRouteAnchorSegment);
}

function nextRouteAnchorSegment<T extends { kind: string }>(day: { segments: T[] }, segmentIndex: number) {
  return day.segments.slice(segmentIndex + 1).find(isRouteAnchorSegment);
}

function segmentExists(plan: ItineraryPlan | null, segmentId?: string | null) {
  return Boolean(plan && segmentId && plan.days.some((day) => day.segments.some((segment) => segment.id === segmentId)));
}

function upsertAgentSessionSummary(
  sessions: ReturnType<typeof plannerStore.getSnapshot>["agentSessions"],
  session: AgentSession
) {
  const summary = {
    sessionId: session.sessionId,
    status: session.status,
    city: session.city,
    title: session.title,
    activePlanId: session.activePlanId,
    activeVersionId: session.activeVersionId ?? null,
    turnCount: session.turns.length,
    updatedAt: new Date().toISOString(),
    createdAt: session.turns[0]?.createdAt ?? new Date().toISOString()
  };
  return [summary, ...sessions.filter((item) => item.sessionId !== session.sessionId)];
}

function isVersionConflict(error: unknown) {
  return error instanceof ApiError && error.status === 409;
}

function routeErrorMessage(error: unknown) {
  if (error instanceof ApiError) {
    if (error.status === 400) {
      return "路线选择未通过校验，请基于最新路线重新选择。";
    }
    if (error.status === 404) {
      return "当前路线或行程已不存在，请刷新后重新选择。";
    }
    if (error.status >= 500) {
      return "路线服务暂时不可用，路线选择未保存，请稍后重试。";
    }
  }
  return error instanceof Error ? error.message : "路线保存失败，请稍后重试。";
}

function sortRouteCandidates(routes: RouteOption[]) {
  return [...routes].sort((left, right) => {
    const durationDiff = routeDurationSeconds(left) - routeDurationSeconds(right);
    if (durationDiff !== 0) {
      return durationDiff;
    }
    const costDiff = (left.costAmount ?? left.costEstimate ?? 0) - (right.costAmount ?? right.costEstimate ?? 0);
    if (costDiff !== 0) {
      return costDiff;
    }
    return (left.sortOrder ?? 0) - (right.sortOrder ?? 0);
  });
}

function selectedRouteForCandidates(routes: RouteOption[]) {
  return routes.find((route) => route.isSelected) ?? routes[0] ?? null;
}

function isDisplayableRoute(route: RouteOption) {
  if (route.error) {
    return false;
  }
  const status = String(route.routeStatus ?? route.status ?? route.providerPayload?.routeStatus ?? route.providerPayload?.status ?? "");
  if (["waiting_for_poi_grounding", "provider_rate_limited", "route_skipped_not_enough_anchors"].includes(status)) {
    return false;
  }
  return true;
}

function routeDurationSeconds(route: RouteOption) {
  if (route.durationSeconds && route.durationSeconds > 0) {
    return route.durationSeconds;
  }
  return (route.durationMinutes ?? 0) * 60;
}

function RouteCandidatePanel({
  candidates,
  hasNextSegment,
  isRefreshing,
  onPreview,
  onRefreshDriving,
  onUse,
  routeColorMap,
  savingRouteId
}: {
  candidates: RouteOption[];
  hasNextSegment: boolean;
  isRefreshing: boolean;
  onPreview: (route: RouteDisplayGroup) => void;
  onRefreshDriving: () => void;
  onUse: (route: RouteDisplayGroup) => void;
  routeColorMap: RouteColorMap;
  savingRouteId: string | null;
}) {
  if (!hasNextSegment) {
    return <p className="route-candidate-empty">这是当天最后一个地点。</p>;
  }
  if (!candidates.length) {
    return (
      <div className="route-candidate-panel" aria-label="路线候选">
        <button
          className="route-refresh-button"
          disabled={isRefreshing}
          onClick={onRefreshDriving}
          type="button"
        >
          {isRefreshing ? "刷新中" : "生成驾车/打车"}
        </button>
        <p className="route-candidate-empty">路线待地点确认后生成。</p>
      </div>
    );
  }
  const displayCandidates = groupedRouteCandidates(candidates);
  const hasDrivingCandidate = displayCandidates.some((group) => group.modes.some(isDrivingTaxiMode));
  return (
    <div className="route-candidate-panel" aria-label="路线候选">
      {!hasDrivingCandidate ? (
        <button
          className="route-refresh-button"
          disabled={isRefreshing}
          onClick={onRefreshDriving}
          type="button"
        >
          {isRefreshing ? "刷新中" : "生成驾车/打车"}
        </button>
      ) : null}
      {displayCandidates.map((group) => (
        <article
          className={`route-candidate-card ${group.isSelected ? "selected" : ""}`}
          key={group.id}
          style={{ "--route-color": routeLegColor(group.representative, routeColorMap) } as CSSProperties}
        >
          <button type="button" onClick={() => onPreview(group)} onFocus={() => onPreview(group)}>
            <span>{group.label}</span>
            {group.isSelected ? <strong>当前路线</strong> : null}
            <small>
              {formatDuration(group.durationMinutes)} · {formatDistance(group.distanceMeters)} · {routeGroupCostLabel(group)}
            </small>
            {routeUserVisibleCaveat(group.representative) ? <small>{routeUserVisibleCaveat(group.representative)}</small> : null}
          </button>
          <button
            className="use-route-button"
            disabled={group.isSelected || savingRouteId === group.id || savingRouteId === group.representativeRouteId || Boolean(group.error)}
            onClick={() => onUse(group)}
            type="button"
          >
            {group.isSelected ? "已使用" : savingRouteId === group.id || savingRouteId === group.representativeRouteId ? "保存中" : "使用此路线"}
          </button>
        </article>
      ))}
    </div>
  );
}

function RouteLegSummary({
  expanded,
  onToggle,
  route,
  routeColorMap
}: {
  expanded: boolean;
  onToggle: () => void;
  route: RouteOption;
  routeColorMap: RouteColorMap;
}) {
  const cost = routeCostLabel(route);
  const caveat = routeUserVisibleCaveat(route);
  return (
    <div className="route-leg-summary" style={{ "--route-color": routeLegColor(route, routeColorMap) } as CSSProperties}>
      <span className="route-color-dot" aria-hidden="true" />
      <span>{routeDisplayLabel(route)}</span>
      <span>{formatDuration(route.durationMinutes)}</span>
      <span>{formatDistance(route.distanceMeters)}</span>
      <span>{cost}</span>
      <span>风险：{routeRiskLabel(route)}</span>
      {caveat ? <span>{caveat}</span> : null}
      <button type="button" onClick={onToggle}>{expanded ? "收起路线" : "展开路线"}</button>
    </div>
  );
}

function routeUserVisibleCaveat(route: RouteOption) {
  const value = route.providerPayload?.userVisibleCaveat;
  return typeof value === "string" ? value : "";
}

function groupedRouteCandidates(candidates: RouteOption[]): RouteDisplayGroup[] {
  const grouped: RouteDisplayGroup[] = [];
  const consumed = new Set<string>();
  for (const route of candidates) {
    if (consumed.has(route.id)) {
      continue;
    }
    if (!isDrivingTaxiMode(route.mode || route.transportMode)) {
      grouped.push(singleRouteDisplayGroup(route));
      consumed.add(route.id);
      continue;
    }
    const sameLeg = candidates.filter(
      (candidate) =>
        !candidate.error &&
        isDrivingTaxiMode(candidate.mode || candidate.transportMode) &&
        candidate.fromSegmentId === route.fromSegmentId &&
        candidate.toSegmentId === route.toSegmentId &&
        candidate.fromPoiId === route.fromPoiId &&
        candidate.toPoiId === route.toPoiId
    );
    sameLeg.forEach((candidate) => consumed.add(candidate.id));
    const representative = sameLeg.find((candidate) => candidate.isSelected) ?? sortRouteCandidates(sameLeg)[0] ?? route;
    const maxCost = Math.max(...sameLeg.map((candidate) => Number(candidate.costAmount ?? candidate.costEstimate ?? 0)), 0);
    const fastest = sortRouteCandidates(sameLeg)[0] ?? representative;
    grouped.push({
      id: `route-group:${representative.fromSegmentId ?? ""}:${representative.toSegmentId ?? ""}:driving-taxi`,
      label: "驾车/打车",
      routeIds: sameLeg.map((candidate) => candidate.id),
      representativeRouteId: representative.id,
      selectedRawRouteId: sameLeg.find((candidate) => candidate.isSelected)?.id,
      durationMinutes: fastest.durationMinutes,
      distanceMeters: fastest.distanceMeters,
      costAmount: maxCost,
      modes: Array.from(new Set(sameLeg.map((candidate) => candidate.mode || candidate.transportMode).filter(Boolean))),
      rawRoutes: sameLeg,
      representative,
      isSelected: sameLeg.some((candidate) => candidate.isSelected),
      error: representative.error
    });
  }
  return grouped;
}

function singleRouteDisplayGroup(route: RouteOption): RouteDisplayGroup {
  return {
    id: route.id,
    label: routeDisplayLabel(route),
    routeIds: [route.id],
    representativeRouteId: route.id,
    selectedRawRouteId: route.isSelected ? route.id : undefined,
    durationMinutes: route.durationMinutes,
    distanceMeters: route.distanceMeters,
    costAmount: Number(route.costAmount ?? route.costEstimate ?? 0),
    modes: [route.mode || route.transportMode].filter(Boolean),
    rawRoutes: [route],
    representative: route,
    isSelected: route.isSelected,
    error: route.error
  };
}

function routeGroupCostLabel(group: RouteDisplayGroup) {
  if (group.modes.some((mode) => ["walking", "walk", "bicycling", "bike", "cycling"].includes(mode))) {
    return "免费";
  }
  if (Number(group.costAmount) > 0) {
    return `¥${Math.round(Number(group.costAmount))}`;
  }
  return routeCostLabel(group.representative);
}

function routeDisplayLabel(route: RouteOption) {
  if (isDrivingTaxiMode(route.mode || route.transportMode)) {
    return "驾车/打车";
  }
  return route.label || transportLabel(route.mode || route.transportMode);
}

function isDrivingTaxiMode(mode: string | undefined) {
  return ["driving", "taxi", "self_drive"].includes(String(mode || ""));
}

function transportLabel(mode: string) {
  return {
    walking: "步行",
    bicycling: "骑行",
    transit: "公交/地铁",
    driving: "驾车",
    taxi: "打车",
    walk: "步行",
    public_transit: "公交/地铁",
    self_drive: "驾车"
  }[mode] ?? mode;
}

function buildTimelinePlanningContext(
  snapshot: ReturnType<typeof plannerStore.getSnapshot>,
  plan: ItineraryPlan,
  extra: { selectedSegmentId?: string | null; patchIntent: string; contextPatch?: Record<string, unknown> }
) {
  return {
    city: snapshot.selectedCity,
    currentPreferenceSummary: effectivePreferenceFromSnapshot(snapshot),
    itineraryPlanId: plan.id,
    activeVersionId: snapshot.activeVersionId,
    itineraryPlan: plan,
    itineraryAgentContext: snapshot.itineraryAgentContext,
    selectedDayNumber: snapshot.selectedDayNumber,
    selectedSegmentId: extra.selectedSegmentId ?? snapshot.selectedSegmentId,
    selectedRouteOptionId: snapshot.selectedRouteOptionId,
    selectedMapPoi: snapshot.selectedMapPoi,
    pendingPoiCandidates: snapshot.pendingPoiCandidates,
    candidateMapPois: snapshot.candidateMapPois,
    patchIntent: extra.patchIntent,
    ...(extra.contextPatch ?? {})
  };
}

function effectivePreferenceFromSnapshot(snapshot: ReturnType<typeof plannerStore.getSnapshot>) {
  return (
    effectivePreferenceMemoryText(snapshot.preferenceCard?.summaryText ?? "") ||
    effectivePreferenceMemoryText(snapshot.preferenceMemory?.memoryText ?? "")
  );
}

function findAddedSegmentId(before: ItineraryPlan, after: ItineraryPlan, operations: ItineraryPatchOperation[]) {
  if (!operations.some((operation) => operation.op === "add_segment")) {
    return null;
  }
  const previousIds = new Set(before.days.flatMap((day) => day.segments.map((segment) => segment.id)));
  for (const day of after.days) {
    const added = day.segments.find((segment) => !previousIds.has(segment.id));
    if (added) {
      return added.id;
    }
  }
  return null;
}

function findRemovedSegmentId(operations: ItineraryPatchOperation[]) {
  return operations.find((operation) => operation.op === "remove_segment")?.segmentId ?? null;
}

function findExpandedSegmentId(operations: ItineraryPatchOperation[]) {
  return operations.find((operation) => operation.op === "expand_area_poi_candidates" || operation.op === "expand_meal_poi_candidates")?.segmentId ?? null;
}

function findReplacedSegmentId(operations: ItineraryPatchOperation[]) {
  return operations.find((operation) =>
    operation.op === "replace_segment_poi_from_candidate" ||
    operation.op === "replace_segment_poi"
  )?.segmentId ?? null;
}

function firstSegmentIdFromTimelinePatch(operations: ItineraryPatchOperation[]) {
  for (const operation of operations) {
    const segmentId = operationSegmentId(operation);
    if (segmentId) {
      return segmentId;
    }
  }
  return null;
}

function canRetryTimelinePatchAfterRefresh(operations: ItineraryPatchOperation[], latestPlan: ItineraryPlan) {
  const retryableOps = new Set([
    "replace_segment_poi_from_candidate",
    "replace_segment_poi",
    "expand_area_poi_candidates",
    "expand_meal_poi_candidates"
  ]);
  return operations.every((operation) => {
    const segmentId = operationSegmentId(operation);
    if (!retryableOps.has(operation.op) || !segmentId) {
      return false;
    }
    return segmentExists(latestPlan, segmentId);
  });
}

function operationSegmentId(operation: ItineraryPatchOperation) {
  if (
    operation.op === "replace_segment_poi_from_candidate" ||
    operation.op === "replace_segment_poi" ||
    operation.op === "expand_area_poi_candidates" ||
    operation.op === "expand_meal_poi_candidates" ||
    operation.op === "remove_segment" ||
    operation.op === "replace_transport_mode" ||
    operation.op === "replace_segment_start_time"
  ) {
    return operation.segmentId;
  }
  return null;
}

function nextSegmentAfterRemoval(
  before: ItineraryPlan,
  after: ItineraryPlan,
  removedSegmentId: string | null,
  previousSelectedSegmentId: string | null
) {
  if (!removedSegmentId) {
    return previousSelectedSegmentId;
  }
  const segments = after.days.flatMap((day) => day.segments);
  if (previousSelectedSegmentId && previousSelectedSegmentId !== removedSegmentId && segments.some((segment) => segment.id === previousSelectedSegmentId)) {
    return previousSelectedSegmentId;
  }
  const beforeDay = before.days.find((day) => day.segments.some((segment) => segment.id === removedSegmentId));
  const removedIndex = beforeDay?.segments.findIndex((segment) => segment.id === removedSegmentId) ?? -1;
  const candidateIds = [
    beforeDay?.segments[removedIndex + 1]?.id,
    beforeDay?.segments[removedIndex - 1]?.id
  ].filter(Boolean);
  for (const candidateId of candidateIds) {
    const match = segments.find((segment) => segment.id === candidateId);
    if (match) {
      return match.id;
    }
  }
  return segments[0]?.id ?? null;
}

function dayNumberForSegment(plan: ItineraryPlan, segmentId: string | null) {
  if (!segmentId) {
    return null;
  }
  return plan.days.find((day) => day.segments.some((segment) => segment.id === segmentId))?.dayNumber ?? null;
}

function dayIdForSegment(plan: ItineraryPlan, segmentId: string | null) {
  if (!segmentId) {
    return null;
  }
  return plan.days.find((day) => day.segments.some((segment) => segment.id === segmentId))?.id ?? null;
}

function summarizeRouteWarnings(plan: ItineraryPlan | null, warnings: string[]) {
  const rawWarnings = Array.from(new Set(warnings.filter(Boolean)));
  const routeSegments = plan?.days.flatMap((day) => day.segments.filter((segment) => segment.kind === "visit" || segment.kind === "activity")) ?? [];
  const matchedPoiCount = routeSegments.filter((segment) => segment.poi.mapReady || segment.poi.routeable || segment.poi.groundingStatus === "verified_amap" || hasUsablePoiCoordinates(segment.poi)).length;
  const pendingPoiCount = routeSegments.filter((segment) => !(segment.poi.mapReady || segment.poi.routeable || segment.poi.groundingStatus === "verified_amap" || hasUsablePoiCoordinates(segment.poi))).length;
  const expectedLegs = expectedRouteLegCount(plan);
  const usableLegs = usableRouteLegCount(plan);
  const pendingLegs = Math.max(0, expectedLegs - usableLegs);
  return {
    hasWarnings: rawWarnings.length > 0,
    rawWarnings,
    summary: `已匹配 ${matchedPoiCount} 个地点，${pendingPoiCount} 个地点待补全；已生成 ${usableLegs} 段路线，${pendingLegs} 段待补全。`
  };
}

function hasUsablePoiCoordinates(poi: ItineraryPlan["days"][number]["segments"][number]["poi"]) {
  const hasCoordinates = Number.isFinite(poi.longitude) && Number.isFinite(poi.latitude);
  return Boolean(hasCoordinates && (poi.amapId || poi.source === "amap-place-search"));
}
function expectedRouteLegCount(plan: ItineraryPlan | null) {
  if (!plan) {
    return 0;
  }
  return plan.days.reduce((total, day) => {
    const routeSegments = day.segments.filter((segment) => segment.kind === "visit" || segment.kind === "activity");
    return total + Math.max(0, routeSegments.length - 1);
  }, 0);
}

function usableRouteLegCount(plan: ItineraryPlan | null) {
  if (!plan) {
    return 0;
  }
  const expectedLegKeys = new Set<string>();
  for (const day of plan.days) {
    const routeSegments = day.segments.filter(isRouteAnchorSegment);
    for (let index = 0; index < routeSegments.length - 1; index += 1) {
      expectedLegKeys.add(`${routeSegments[index].id}:${routeSegments[index + 1].id}`);
    }
  }
  const usableLegKeys = new Set<string>();
  for (const route of plan.routeOptions) {
    if (!route.fromSegmentId || !route.toSegmentId || !isDisplayableRoute(route)) {
      continue;
    }
    const key = `${route.fromSegmentId}:${route.toSegmentId}`;
    if (expectedLegKeys.has(key)) {
      usableLegKeys.add(key);
    }
  }
  return usableLegKeys.size;
}
function groundingStatusLabel(segment: EditableSegment) {
  return groundingStatusLabelForSegmentFields(
    segment.groundingStatus,
    segment.kind,
    segment.intentType,
    segment.poiName,
    segment.agentNotes
  );
}

function segmentNeedsConcretePoi(segment: EditableSegment) {
  if (isExpandableOrdinaryMeal(segment)) {
    return true;
  }
  return concretePoiExpansionStatusValues(segment).some((value) =>
    [
      "area_poi",
      "functional_poi",
      "composite_poi",
      "area_unresolved",
      "provider_rate_limited",
      "waiting_for_poi_grounding"
    ].includes(value)
  );
}

function concretePoiExpansionStatusValues(segment: EditableSegment) {
  return [segment.groundingStatus, segment.poiSpecificity ?? ""].map((value) => String(value || "").trim());
}

function concretePoiActionLabel(segment: EditableSegment) {
  if (segment.kind === "meal" && segment.groundingStatus === "waiting_for_poi_grounding") {
    return "请选择具体餐厅/餐饮地点";
  }
  if (segment.kind === "meal" && segment.groundingStatus === "provider_rate_limited") {
    return "餐饮地点待补全，稍后可重试候选";
  }
  if (segment.kind === "meal" && segment.groundingStatus === "optional_waiting") {
    return "用餐时间 · 可选择顺路餐厅";
  }
  if (isExpandableOrdinaryMeal(segment)) {
    return "用餐时间 · 可展开附近餐饮候选";
  }
  if (segment.groundingStatus === "functional_poi" || segment.poiSpecificity === "functional_poi") {
    return "功能地点需要附近搜索补全";
  }
  return "请选择具体场馆/入口/区域";
}

function concretePoiExpandButtonLabel(segment: EditableSegment) {
  return segment.kind === "meal" ? "选择顺路餐厅" : "展开具体候选";
}

function isExpandableOrdinaryMeal(segment: EditableSegment) {
  if (segment.kind !== "meal") {
    return false;
  }
  if (isGroundedMeal(segment)) {
    return false;
  }
  return ["", "optional_waiting", "not_required", "draft_only"].includes(segment.groundingStatus || "");
}

function pendingPoiCandidatesForSegment(candidates: PendingPoiCandidate[], segmentId: string | null, poiName = "") {
  if (!segmentId && !poiName) {
    return [];
  }
  const normalizedName = normalizeCandidateText(poiName);
  return candidates.filter((candidate) => {
    if (candidate.sourceSegmentId) {
      return candidate.sourceSegmentId === segmentId;
    }
    const normalizedQuery = normalizeCandidateText(candidate.query);
    if (segmentId && normalizedQuery.includes(normalizeCandidateText(segmentId))) {
      return true;
    }
    if (normalizedName && (normalizedQuery.includes(normalizedName) || normalizedName.includes(normalizedQuery))) {
      return true;
    }
    return candidate.candidates.some((poi) => {
      const text = normalizeCandidateText([poi.name, poi.address, poi.district, poi.type].filter(Boolean).join(" "));
      return Boolean(normalizedName && text.includes(normalizedName));
    });
  });
}

function normalizeCandidateText(value: string) {
  return value.replace(/[\s\-_,.()（）·・，。]+/g, "").toLowerCase();
}

function segmentCostLabel(segment: EditableSegment) {
  if (segment.kind === "meal") {
    if (segment.estimatedCost <= 0) {
      return "餐饮费用待估算";
    }
    const mealCost = mealCostMetadata(segment);
    if (mealCost?.basis === "per_person" && mealCost.perPerson > 0) {
      const perPersonLabel = isGroundedMeal(segment) ? `人均约 ¥${Math.round(mealCost.perPerson)}` : `人均预算约 ¥${Math.round(mealCost.perPerson)}`;
      if (mealCost.partySize > 1) {
        const total = mealCost.total > 0 ? mealCost.total : segment.estimatedCost;
        const totalLabel = isGroundedMeal(segment) ? `合计约 ¥${Math.round(total)}` : `合计预算约 ¥${Math.round(total)}`;
        return `${perPersonLabel} · ${totalLabel}`;
      }
      return perPersonLabel;
    }
    return isGroundedMeal(segment) ? `约 ¥${Math.round(segment.estimatedCost)}` : `餐饮预算约 ¥${Math.round(segment.estimatedCost)}`;
  }
  return segment.estimatedCost > 0 ? `¥${Math.round(segment.estimatedCost)}` : "景点费用待预约确认";
}

function mealCostMetadata(segment: EditableSegment): { basis: string; perPerson: number; partySize: number; total: number } | null {
  const text = `${segment.agentNotes || ""} ${segment.poiSourceNote || ""}`;
  if (!/costBasis\s*=\s*per_person/i.test(text)) {
    return null;
  }
  const perPerson = numberMarker(text, "costPerPerson");
  const partySize = numberMarker(text, "partySize") || 1;
  const total = numberMarker(text, "totalCost") || segment.estimatedCost;
  if (!perPerson) {
    return null;
  }
  return {
    basis: "per_person",
    perPerson,
    partySize: Math.max(1, Math.round(partySize)),
    total
  };
}

function numberMarker(text: string, key: string) {
  const match = new RegExp(`${key}\\s*=\\s*(\\d+(?:\\.\\d+)?)`, "i").exec(text);
  return match ? Number(match[1]) : 0;
}

function isGroundedMeal(segment: EditableSegment) {
  return segment.kind === "meal" && segment.poiSource === "amap-place-search" && (segment.mapReady || segment.routeable);
}

function durationRangeLabel(segment: EditableSegment) {
  const duration = segment.estimateMetadata?.duration;
  if (!duration || typeof duration !== "object") {
    return formatDuration(segment.durationMinutes);
  }
  const value = duration as Record<string, unknown>;
  const min = typeof value.minMinutes === "number" ? value.minMinutes : null;
  const preferred = typeof value.preferredMinutes === "number" ? value.preferredMinutes : segment.durationMinutes;
  const max = typeof value.maxMinutes === "number" ? value.maxMinutes : null;
  if (min !== null && max !== null && min !== max) {
    return `建议 ${formatDuration(min)}～${formatDuration(max)}（约 ${formatDuration(preferred)}）`;
  }
  return formatDuration(preferred);
}

function routeCostLabel(route: RouteOption) {
  if (route.error) {
    return "路线待补全";
  }
  const mode = route.mode || route.transportMode;
  if (["walking", "walk", "bicycling", "bike", "cycling"].includes(mode)) {
    return "免费";
  }
  return route.costAmount > 0 ? `¥${Math.round(route.costAmount)}` : "费用未返回";
}

function copySourceLabel(source: string | undefined, fallback: string) {
  if (!source) {
    return fallback;
  }
  if (source.includes("高德")) {
    return source;
  }
  if (source.includes("mock")) {
    return `${fallback}（真实数据待接入）`;
  }
  return source;
}

function copyRiskLevelLabel(level: string | undefined) {
  const labels: Record<string, string> = {
    high: "高风险",
    medium: "中风险",
    low: "低风险",
    risky: "有风险",
    ideal: "适宜",
    neutral: "影响较低",
    unknown: "待判断",
    unavailable: "不可用",
    bad_weather: "天气风险"
  };
  return labels[level ?? ""] ?? level ?? "待判断";
}

function copyWeatherRiskLevelLabel(weather: NonNullable<ItineraryPlan["weatherSignals"][number]>) {
  if (isCopyLowLikeRisk(weather.riskLevel) && !hasReliableCopyWeatherRisk(weather)) {
    return "风险待核验";
  }
  return copyRiskLevelLabel(weather.riskLevel);
}

function copyTrafficRiskSummary(traffic: NonNullable<ItineraryPlan["trafficCrowdingSignals"][number]>) {
  const label = copyCrowdingRiskLabel(traffic);
  return label === "风险待核验" ? "风险待核验" : `${label} · ${traffic.recommendedDepartureAdjustment}`;
}

function copyCrowdingRiskLabel(traffic: NonNullable<ItineraryPlan["trafficCrowdingSignals"][number]>) {
  if (isCopyLowLikeRisk(traffic.crowdingLevel) && !hasReliableCopyTrafficRisk(traffic)) {
    return "风险待核验";
  }
  return copyRiskLevelLabel(traffic.crowdingLevel);
}

function isCopyLowLikeRisk(level: string | undefined) {
  return ["low", "ideal", "neutral"].includes(String(level || "").toLowerCase());
}

function hasReliableCopyWeatherRisk(weather: NonNullable<ItineraryPlan["weatherSignals"][number]>) {
  const status = String(weather.dataStatus || "").toLowerCase();
  const source = `${weather.source || ""} ${weather.providerName || ""}`.toLowerCase();
  if (weather.fallbackUsed || weather.failureReason) {
    return false;
  }
  if (!status || ["fallback", "unavailable", "pending", "not_checked", "not_required"].includes(status)) {
    return false;
  }
  return !/(mock|unavailable|not configured|provider unavailable)/i.test(source);
}

function hasReliableCopyTrafficRisk(traffic: NonNullable<ItineraryPlan["trafficCrowdingSignals"][number]>) {
  const source = String(traffic.source || "").toLowerCase();
  return Boolean(traffic.realDataAvailable) && !/(mock|unavailable|not configured|provider unavailable)/i.test(source);
}

function copyStatusLabel(status: string | undefined) {
  const labels: Record<string, string> = {
    available: "已查询",
    degraded: "部分数据可用",
    unavailable: "不可用",
    fallback: "已降级",
    pending: "待核验",
    not_checked: "未查询",
    not_required: "无需实时风险"
  };
  return labels[status ?? ""] ?? "待查询";
}

function copyPoiRiskStatusLabel(alert: NonNullable<ItineraryPlan["poiRiskAlerts"]>[number]) {
  if (copyPoiRiskNeedsVerification(alert)) {
    return "风险待核验";
  }
  return copyStatusLabel(alert.status);
}

function copyPoiRiskNeedsVerification(alert: NonNullable<ItineraryPlan["poiRiskAlerts"]>[number]) {
  const diagnostics = copyRiskSearchDiagnostics(alert);
  const acceptedCount = copyNumericDiagnostic(diagnostics?.acceptedSourceCount);
  const reason = String(diagnostics?.riskStatusReason || alert.failureReason || "");
  const status = String(alert.status || "").toLowerCase();
  const noReliableSourceReasons = new Set([
    "search_provider_unavailable",
    "search_no_results",
    "search_results_all_stale",
    "search_results_low_credibility",
    "all_web_search_providers_failed_or_empty"
  ]);
  if (noReliableSourceReasons.has(reason)) {
    return true;
  }
  if ((status === "degraded" || status === "unavailable") && acceptedCount === 0) {
    return true;
  }
  if (status === "unavailable" && !alert.sourceUrl && (acceptedCount === undefined || acceptedCount <= 0)) {
    return true;
  }
  return false;
}

function copyRiskSourceStatsSummary(alert: NonNullable<ItineraryPlan["poiRiskAlerts"]>[number]) {
  const diagnostics = copyRiskSearchDiagnostics(alert);
  const visibleSources = alert.sources.filter((source) => !source.type && (source.title || source.url || source.snippet));
  const accepted = copyNumericDiagnostic(diagnostics?.acceptedSourceCount) ?? visibleSources.length;
  const official =
    copyNumericDiagnostic(diagnostics?.officialSourceCount) ??
    visibleSources.filter((source) => String(source.credibilityRank || "").toLowerCase() === "official").length;
  const sourceCount = copyNumericDiagnostic(diagnostics?.sourceCount) ?? accepted;
  const stale = copyNumericDiagnostic(diagnostics?.rejectedStaleSourceCount) ?? 0;
  const rejected = Math.max(0, sourceCount - accepted);
  if (!diagnostics && !visibleSources.length) {
    return "";
  }
  return `已采纳来源 ${accepted} 条；官方来源 ${official} 条；已拒绝过期/低相关 ${rejected} 条${stale ? `（过期 ${stale} 条）` : ""}`;
}

function copyRiskSearchDiagnostics(alert: NonNullable<ItineraryPlan["poiRiskAlerts"]>[number]) {
  return alert.sources.find((source) => source.type === "riskSearchDiagnostics");
}

function copyWebSearchProviderDiagnostics(alert: NonNullable<ItineraryPlan["poiRiskAlerts"]>[number]) {
  return alert.sources.find((source) => source.type === "webSearchProviderDiagnostics");
}

function formatPoiRiskDebugForCopy(alert: NonNullable<ItineraryPlan["poiRiskAlerts"]>[number]) {
  const debugEntries: Array<[string, unknown]> = [];
  const riskDiagnostics = copyRiskSearchDiagnostics(alert);
  const providerDiagnostics = copyWebSearchProviderDiagnostics(alert);
  if (riskDiagnostics) {
    debugEntries.push(["riskSearchDiagnostics", riskDiagnostics]);
  }
  if (providerDiagnostics) {
    debugEntries.push(["webSearchProviderDiagnostics", providerDiagnostics]);
  }
  if (!debugEntries.length) {
    return [];
  }
  return debugEntries.flatMap(([label, value]) => [
    `${label}:`,
    ...safeDebugJson(value).split("\n").map((line) => `  ${line}`)
  ]);
}

function safeDebugJson(value: unknown) {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function copyNumericDiagnostic(value: unknown) {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function friendlyRiskSearchMessage(reason: string | null | undefined) {
  if (!reason) {
    return "搜索状态待确认。";
  }
  const labels: Record<string, string> = {
    search_provider_unavailable: "搜索供应商未配置或暂不可用，当前无法自动核验官方公告。",
    search_no_results: "未找到可用公开搜索结果。",
    search_results_all_stale: "找到公开结果，但未满足当前出行日期要求。",
    search_results_low_credibility: "找到公开结果，但未满足官方来源或可信度要求。",
    search_success_degraded: "部分搜索供应商失败或跳过，当前风险判断仍需核对官方公告。",
    all_web_search_providers_failed_or_empty: "所有搜索供应商均失败、跳过或未返回可用结果。"
  };
  if (labels[reason]) {
    return labels[reason];
  }
  if (/KEY|TOKEN|SECRET|API|PROVIDER|configured|not configured/i.test(reason)) {
    return "搜索供应商未配置，当前无法自动核验官方公告。";
  }
  return friendlyTimelineMessage(reason);
}

function copyProviderDiagnosticsSummary(alert: NonNullable<ItineraryPlan["poiRiskAlerts"]>[number]) {
  const diagnosticsSource = alert.sources.find((source) => source.type === "webSearchProviderDiagnostics");
  const riskDiagnostics = alert.sources.find((source) => source.type === "riskSearchDiagnostics");
  const providerDiagnostics = Array.isArray(diagnosticsSource?.providerDiagnostics) ? diagnosticsSource.providerDiagnostics : [];
  if (!providerDiagnostics.length) {
    return "";
  }
  const allSkippedMissingConfig = providerDiagnostics.every(
    (item) => String(item.status) === "skipped" && String(item.reason) === "skipped_missing_config"
  );
  if (allSkippedMissingConfig) {
    return "搜索供应商未配置，当前无法自动核验官方公告。";
  }
  const parts = providerDiagnostics.slice(0, 6).map((item) => {
    const provider = copyProviderDisplayName(String(item.providerName || ""));
    const status = String(item.status || "");
    const reason = String(item.reason || "");
    const resultCount = typeof item.resultCount === "number" ? item.resultCount : 0;
    if (status === "success") {
      return `${provider} 成功 ${resultCount} 条`;
    }
    if (status === "skipped" && reason === "skipped_missing_config") {
      return `${provider} 未配置`;
    }
    if (status === "skipped") {
      return `${provider} 跳过`;
    }
    if (status === "no_results" || reason === "no_usable_results") {
      return `${provider} 无可用结果`;
    }
    if (reason === "timeout") {
      return `${provider} 超时`;
    }
    if (status === "cache_hit") {
      return `${provider} 命中缓存`;
    }
    return `${provider} 失败`;
  });
  const statusReason = String(riskDiagnostics?.riskStatusReason || alert.failureReason || "");
  if (statusReason === "search_results_all_stale") {
    parts.push("公开结果未满足当前出行日期要求");
  } else if (statusReason === "search_results_low_credibility") {
    parts.push("公开结果未满足官方来源或可信度要求");
  }
  return parts.join("；");
}

function copyProviderDisplayName(providerName: string) {
  const labels: Record<string, string> = {
    "bocha-web-search": "Bocha",
    tavily: "Tavily",
    "brave-web-search": "Brave",
    searxng: "SearXNG",
    "google-cse": "Google CSE",
    "multi-free-search": "Multi-free",
    "baidu-html-search": "Baidu",
    "duckduckgo-html-search": "DuckDuckGo",
    "chained-web-search": "Provider chain"
  };
  return labels[providerName] ?? (providerName || "搜索供应商");
}

function timelineOrder(value: string) {
  const match = String(value || "").match(/(\d{1,2}):(\d{2})/);
  if (!match) {
    return 24 * 60;
  }
  return Number(match[1]) * 60 + Number(match[2]);
}

function pendingSlotTimeLabel(slot: PendingTimelineSlot) {
  if (slot.timingStatus === "time_pending") {
    return "时间待定";
  }
  if (slot.timeWindow && /\d{1,2}:\d{2}/.test(slot.timeWindow)) {
    return slot.timeWindow;
  }
  return [slot.startTime, slot.endTime].filter(Boolean).join("–") || "时间待定";
}

function pendingSlotTimelineOrder(slot: PendingTimelineSlot, segments: EditableSegment[]) {
  if (slot.startTime) {
    return timelineOrder(slot.startTime);
  }
  const before = segments.find((segment) => segment.id === slot.placementBeforeSegmentId);
  if (before) {
    return Math.max(0, timelineOrder(before.startTime) - 1);
  }
  const after = segments.find((segment) => segment.id === slot.placementAfterSegmentId);
  if (after) {
    return timelineOrder(after.endTime || after.startTime) + 1;
  }
  return 0;
}

function formatQueryTime(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value || "待查询";
  }
  return date.toLocaleString("zh-CN", { hour12: false });
}

function formatCopyWeatherDate(value: string) {
  const date = new Date(`${value}T00:00:00`);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleDateString("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit" });
}

function friendlyTimelineMessage(message: string) {
  if (!message) {
    return "";
  }
  if (/KEY|TOKEN|SECRET|API|PROVIDER|provider|mock|fallback|AMAP|WEB_SERVICE|configured|not configured|bocha/i.test(message)) {
    return "相关服务暂时不可用，当前结果可能不完整；请稍后重新查询或检查服务配置。";
  }
  return message.replace(/\bfallback\b/gi, "降级");
}

function FallbackRiskSignals() {
  return (
    <section aria-label="Weather and crowding signals" className="risk-signals">
      <div className="risk-signals-header">
        <div>
          <h3>风险提示</h3>
          <p className="risk-signals-summary">天气：待 Agent 查询 · 景区风险：待 Agent 查询</p>
        </div>
      </div>
      <p>天气：待 Agent 在识别到具体旅行日期后联网查询。</p>
      <p>景区风险：待 Agent 结合景区公开信息生成提示。</p>
    </section>
  );
}

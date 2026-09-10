import {
  BudgetBreakdown,
  ItineraryPlan,
  PendingTimelineSlot,
  PlannerSegment,
  RouteOption
} from "../../services/apiClient";
import { isRouteAnchorSegment } from "./routeAnchors";

export type EditableSegment = {
  id: string;
  dayId: string;
  startTime: string;
  endTime: string;
  poiName: string;
  agentNotes: string;
  durationMinutes: number;
  estimatedCost: number;
  estimateMetadata?: Record<string, unknown>;
  routeCost: number;
  routeDurationMinutes: number;
  walkingDistanceMeters: number;
  routeLegRequired: boolean;
  routeLegCovered: boolean;
  kind: string;
  transportMode: string;
  poiSource: string;
  poiSourceNote?: string;
  poiConfidence: number;
  groundingStatus: string;
  mapReady: boolean;
  routeable: boolean;
  matchedAmapName?: string | null;
  poiSpecificity?: string;
  intentType?: string | null;
  needsConcretePoi?: boolean;
};

export type EditableDay = {
  id: string;
  dayNumber: number;
  dayTitle: string;
  collapsed: boolean;
  segments: EditableSegment[];
  pendingSlots: PendingTimelineSlot[];
};

export type DayTotals = {
  walkingDistanceMeters: number;
  /** Compatibility total: all scheduled segment dwell time, including explicit buffers. */
  durationMinutes: number;
  activityDurationMinutes?: number;
  travelDurationMinutes?: number;
  bufferDurationMinutes?: number;
  scheduleSpanMinutes?: number;
  explicitBufferMinutes?: number;
  unallocatedGapMinutes?: number;
  requiredRouteLegCount?: number;
  coveredRouteLegCount?: number;
  unknownRouteLegCount?: number;
  chronologyViolationCount?: number;
  overlapCount?: number;
  overlapMinutes?: number;
  estimatedCost: number;
};

export type TripTotals = DayTotals;

export function authoritativeBudgetSummary(plan: ItineraryPlan, localEstimatedCost = 0): BudgetBreakdown {
  if (plan.budgetBreakdown) {
    return plan.budgetBreakdown;
  }
  const fallback = plan.budgetEstimate > 0 ? plan.budgetEstimate : localEstimatedCost;
  return {
    tier: plan.budgetTier ?? "unknown",
    numericTarget: plan.budgetTarget ?? null,
    knownActivityCost: 0,
    knownMealCost: 0,
    knownTransportCost: 0,
    knownTotal: fallback,
    provisionalMin: fallback,
    provisionalPreferred: fallback,
    provisionalMax: fallback,
    unknownItems: [],
    isComplete: true
  };
}

export type ItineraryAgentContext = {
  tripTitle: string;
  days: Array<{
    dayNumber: number;
    dayTitle: string;
    segments: Array<{
      time: string;
      poiName: string;
      agentNotes: string;
      duration: number;
      estimatedCost: number;
    }>;
    dayTotals: DayTotals;
  }>;
  tripTotals: TripTotals;
};

export function createEditableDays(plan: ItineraryPlan | null): EditableDay[] {
  const sourceDays = plan?.days ?? [];

  return sourceDays.map((day) => ({
    id: day.id,
    dayNumber: day.dayNumber,
    dayTitle: day.title || defaultDayTitle(day.dayNumber),
    collapsed: false,
    pendingSlots: day.pendingSlots ?? [],
    segments: day.segments.map((segment, index) =>
      createEditableSegment(
        day.id,
        segment,
        selectedRouteForSegment(plan, day, index),
        isRouteAnchorSegment(segment) && Boolean(nextRouteAnchorSegment(day, index))
      )
    )
  }));
}

export function createSkeletonSegment(dayId: string, dayNumber: number, previous?: EditableSegment): EditableSegment {
  const startTime = previous ? addMinutes(previous.endTime, 30) : "09:30";
  const safeStartTime = startTime > "23:00" ? "09:30" : startTime;
  return {
    id: `draft_day_${dayNumber}_seg_${Date.now()}`,
    dayId,
    startTime: safeStartTime,
    endTime: addMinutes(safeStartTime, 30),
    poiName: "待定景点/活动",
    agentNotes: "等待 Agent 补全景点、预约、天气和风险信息。",
    durationMinutes: 30,
    estimatedCost: 0,
    routeCost: 0,
    routeDurationMinutes: 0,
    walkingDistanceMeters: 0,
    routeLegRequired: false,
    routeLegCovered: false,
    kind: "activity",
    transportMode: "walk",
    poiSource: "manual-draft",
    poiSourceNote: "手动创建的待补全活动。",
    poiConfidence: 0,
    groundingStatus: "draft_only",
    mapReady: false,
    routeable: false,
    matchedAmapName: null,
    poiSpecificity: "exact_entity",
    intentType: null,
    needsConcretePoi: false
  };
}

export function createEmptyDay(dayNumber: number): EditableDay {
  const dayId = `draft_day_${dayNumber}_${Date.now()}`;
  return {
    id: dayId,
    dayNumber,
    dayTitle: "待规划日程",
    collapsed: false,
    pendingSlots: [],
    segments: []
  };
}

export function calculateDayTotals(day: EditableDay): DayTotals {
  const totals = day.segments.reduce(
    (totals, segment) => ({
      walkingDistanceMeters: totals.walkingDistanceMeters + segment.walkingDistanceMeters,
      durationMinutes: totals.durationMinutes + segment.durationMinutes,
      activityDurationMinutes:
        totals.activityDurationMinutes + (isBufferSegment(segment) ? 0 : segment.durationMinutes),
      travelDurationMinutes: totals.travelDurationMinutes + segment.routeDurationMinutes,
      bufferDurationMinutes: totals.bufferDurationMinutes + (isBufferSegment(segment) ? segment.durationMinutes : 0),
      scheduleSpanMinutes: 0,
      explicitBufferMinutes: totals.explicitBufferMinutes + (isBufferSegment(segment) ? segment.durationMinutes : 0),
      unallocatedGapMinutes: 0,
      requiredRouteLegCount: totals.requiredRouteLegCount + (segment.routeLegRequired ? 1 : 0),
      coveredRouteLegCount: totals.coveredRouteLegCount + (segment.routeLegCovered ? 1 : 0),
      unknownRouteLegCount: totals.unknownRouteLegCount + (segment.routeLegRequired && !segment.routeLegCovered ? 1 : 0),
      estimatedCost: totals.estimatedCost + segment.estimatedCost + segment.routeCost
    }),
    {
      walkingDistanceMeters: 0,
      durationMinutes: 0,
      activityDurationMinutes: 0,
      travelDurationMinutes: 0,
      bufferDurationMinutes: 0,
      scheduleSpanMinutes: 0,
      explicitBufferMinutes: 0,
      unallocatedGapMinutes: 0,
      requiredRouteLegCount: 0,
      coveredRouteLegCount: 0,
      unknownRouteLegCount: 0,
      estimatedCost: 0
    }
  );
  const starts = day.segments.map((segment) => toMinutes(segment.startTime));
  const ends = day.segments.map((segment) => toMinutes(segment.endTime));
  const scheduleSpanMinutes = starts.length ? Math.max(0, Math.max(...ends) - Math.min(...starts)) : 0;
  const intervals = day.segments.map((segment, index) => ({
    start: toMinutes(segment.startTime),
    end: toMinutes(segment.endTime),
    index
  }));
  const chronologyViolationCount = intervals.reduce(
    (count, interval, index) => count + (index > 0 && interval.start < intervals[index - 1].start ? 1 : 0),
    0
  );
  let overlapCount = 0;
  for (let left = 0; left < intervals.length; left += 1) {
    for (let right = left + 1; right < intervals.length; right += 1) {
      if (Math.min(intervals[left].end, intervals[right].end) > Math.max(intervals[left].start, intervals[right].start)) overlapCount += 1;
    }
  }
  const events = intervals.flatMap((interval) => [{ at: interval.start, delta: 1 }, { at: interval.end, delta: -1 }])
    .sort((left, right) => left.at - right.at || left.delta - right.delta);
  let active = 0;
  let previous = 0;
  let overlapMinutes = 0;
  events.forEach((event) => {
    if (active > 1) overlapMinutes += event.at - previous;
    active += event.delta;
    previous = event.at;
  });
  return {
    ...totals,
    scheduleSpanMinutes,
    unallocatedGapMinutes: Math.max(
      0,
      scheduleSpanMinutes - totals.activityDurationMinutes - totals.bufferDurationMinutes - totals.travelDurationMinutes + overlapMinutes
    ),
    chronologyViolationCount,
    overlapCount,
    overlapMinutes
  };
}

export function calculateTripTotals(days: EditableDay[]): TripTotals {
  return days.reduce(
    (totals, day) => {
      const dayTotals = calculateDayTotals(day);
      return {
        walkingDistanceMeters: totals.walkingDistanceMeters + dayTotals.walkingDistanceMeters,
        durationMinutes: totals.durationMinutes + dayTotals.durationMinutes,
        activityDurationMinutes: (totals.activityDurationMinutes ?? 0) + (dayTotals.activityDurationMinutes ?? 0),
        travelDurationMinutes: (totals.travelDurationMinutes ?? 0) + (dayTotals.travelDurationMinutes ?? 0),
        bufferDurationMinutes: (totals.bufferDurationMinutes ?? 0) + (dayTotals.bufferDurationMinutes ?? 0),
        scheduleSpanMinutes: (totals.scheduleSpanMinutes ?? 0) + (dayTotals.scheduleSpanMinutes ?? 0),
        explicitBufferMinutes: (totals.explicitBufferMinutes ?? 0) + (dayTotals.explicitBufferMinutes ?? 0),
        unallocatedGapMinutes: (totals.unallocatedGapMinutes ?? 0) + (dayTotals.unallocatedGapMinutes ?? 0),
        requiredRouteLegCount: (totals.requiredRouteLegCount ?? 0) + (dayTotals.requiredRouteLegCount ?? 0),
        coveredRouteLegCount: (totals.coveredRouteLegCount ?? 0) + (dayTotals.coveredRouteLegCount ?? 0),
        unknownRouteLegCount: (totals.unknownRouteLegCount ?? 0) + (dayTotals.unknownRouteLegCount ?? 0),
        chronologyViolationCount: (totals.chronologyViolationCount ?? 0) + (dayTotals.chronologyViolationCount ?? 0),
        overlapCount: (totals.overlapCount ?? 0) + (dayTotals.overlapCount ?? 0),
        overlapMinutes: (totals.overlapMinutes ?? 0) + (dayTotals.overlapMinutes ?? 0),
        estimatedCost: totals.estimatedCost + dayTotals.estimatedCost
      };
    },
    {
      walkingDistanceMeters: 0,
      durationMinutes: 0,
      activityDurationMinutes: 0,
      travelDurationMinutes: 0,
      bufferDurationMinutes: 0,
      scheduleSpanMinutes: 0,
      explicitBufferMinutes: 0,
      unallocatedGapMinutes: 0,
      requiredRouteLegCount: 0,
      coveredRouteLegCount: 0,
      unknownRouteLegCount: 0,
      chronologyViolationCount: 0,
      overlapCount: 0,
      overlapMinutes: 0,
      estimatedCost: 0
    }
  );
}

export function buildItineraryAgentContext(tripTitle: string, days: EditableDay[]): ItineraryAgentContext {
  return {
    tripTitle,
    days: days.map((day) => ({
      dayNumber: day.dayNumber,
      dayTitle: day.dayTitle,
      segments: day.segments.map((segment) => ({
        time: segment.startTime,
        poiName: segment.poiName,
        agentNotes: segment.agentNotes,
        duration: segment.durationMinutes,
        estimatedCost: segment.estimatedCost
      })),
      dayTotals: calculateDayTotals(day)
    })),
    tripTotals: calculateTripTotals(days)
  };
}

export function validateTripTitle(value: string): string {
  const normalized = value.trim();
  if (!normalized) {
    return "旅行标题不能为空。";
  }
  if (normalized.length > 32) {
    return "旅行标题不能超过 32 个字。";
  }
  return "";
}

export function validateDayTitle(value: string): string {
  const normalized = value.trim();
  if (!normalized) {
    return "Day 标题不能为空。";
  }
  if (normalized.length > 32) {
    return "Day 标题不能超过 32 个字。";
  }
  return "";
}

export function validateSegmentStartTime(day: EditableDay, segmentId: string, nextStartTime: string): string {
  if (!isValidClockTime(nextStartTime)) {
    return "时间格式必须为 HH:mm，例如 09:30。";
  }
  const segmentIndex = day.segments.findIndex((segment) => segment.id === segmentId);
  const segment = day.segments[segmentIndex];
  if (!segment) {
    return "未找到当前活动。";
  }

  const nextEndTime = addMinutes(nextStartTime, segment.durationMinutes);
  if (toMinutes(nextEndTime) <= toMinutes(nextStartTime)) {
    return "活动结束时间不能跨日，请调整时间点。";
  }

  const previous = day.segments[segmentIndex - 1];
  if (previous && toMinutes(nextStartTime) < toMinutes(previous.endTime)) {
    return "时间与上一项冲突，不能早于上一项结束时间。";
  }

  const next = day.segments[segmentIndex + 1];
  if (next && toMinutes(nextEndTime) > toMinutes(next.startTime)) {
    return "时间与下一项冲突，请保留足够间隔。";
  }

  return "";
}

export function addMinutes(value: string, minutes: number): string {
  const total = toMinutes(value) + minutes;
  const hours = Math.floor(total / 60);
  const mins = total % 60;
  return `${String(hours).padStart(2, "0")}:${String(mins).padStart(2, "0")}`;
}

export function formatDistance(meters: number): string {
  return `${(meters / 1000).toFixed(1)} km`;
}

export function formatDuration(minutes: number): string {
  const safeMinutes = Math.max(0, Math.round(minutes));
  const hours = Math.floor(safeMinutes / 60);
  const remainder = safeMinutes % 60;
  return hours > 0 ? `${hours} 小时${remainder} 分钟` : `${remainder} 分钟`;
}

function createEditableSegment(dayId: string, segment: PlannerSegment, route?: RouteOption, routeLegRequired = false): EditableSegment {
  const durationMinutes = Math.max(15, toMinutes(segment.endTime) - toMinutes(segment.startTime));
  return {
    id: segment.id,
    dayId,
    startTime: segment.startTime,
    endTime: segment.endTime,
    poiName: segment.poi.name,
    agentNotes: segment.notes,
    durationMinutes,
    estimatedCost: segment.estimatedCost,
    estimateMetadata: segment.estimateMetadata,
    routeCost: route?.costAmount ?? route?.costEstimate ?? 0,
    routeDurationMinutes: Math.round(routeDurationSeconds(route) / 60),
    walkingDistanceMeters: route?.distanceMeters ?? 0,
    routeLegRequired,
    routeLegCovered: Boolean(route),
    kind: segment.kind,
    transportMode: segment.transportMode,
    poiSource: segment.poi.source,
    poiSourceNote: segment.poi.sourceNote,
    poiConfidence: segment.poi.confidence,
    groundingStatus: segment.poi.groundingStatus ?? segment.poi.grounding?.groundingStatus ?? segment.poi.grounding?.status ?? "",
    mapReady: segment.poi.mapReady ?? segment.poi.grounding?.mapReady ?? false,
    routeable: segment.poi.routeable ?? segment.poi.grounding?.routeable ?? false,
    matchedAmapName: segment.poi.matchedAmapName ?? segment.poi.grounding?.matchedAmapName ?? null,
    poiSpecificity: segment.poi.poiSpecificity ?? segment.poi.grounding?.poiSpecificity ?? "exact_entity",
    intentType: segment.poi.intentType ?? segment.poi.grounding?.intentType ?? null,
    needsConcretePoi: segment.poi.needsConcretePoi ?? segment.poi.grounding?.needsConcretePoi ?? false
  };
}

function selectedRouteForSegment(
  plan: ItineraryPlan | null,
  day: NonNullable<ItineraryPlan["days"][number]>,
  segmentIndex: number
): RouteOption | undefined {
  const current = day.segments[segmentIndex];
  const next = nextRouteAnchorSegment(day, segmentIndex);
  if (!plan || !current || !isRouteAnchorSegment(current) || !next) {
    return undefined;
  }
  const directCandidates = plan.routeOptions.filter(
    (route) => route.fromSegmentId === current.id && route.toSegmentId === next.id && isDisplayableRoute(route)
  );
  return directCandidates.find((route) => route.isSelected) ?? sortRouteCandidates(directCandidates)[0];
}

function nextRouteAnchorSegment(day: NonNullable<ItineraryPlan["days"][number]>, segmentIndex: number): PlannerSegment | undefined {
  return day.segments.slice(segmentIndex + 1).find(isRouteAnchorSegment);
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

function routeDurationSeconds(route?: RouteOption) {
  if (!route) {
    return 0;
  }
  if (route.durationSeconds && route.durationSeconds > 0) {
    return route.durationSeconds;
  }
  return (route.durationMinutes ?? 0) * 60;
}

function isDisplayableRoute(route: RouteOption) {
  if (route.error) {
    return false;
  }
  const status = String(route.routeStatus ?? route.status ?? route.providerPayload?.routeStatus ?? route.providerPayload?.status ?? "");
  return !["waiting_for_poi_grounding", "provider_rate_limited", "route_skipped_not_enough_anchors"].includes(status);
}

function isBufferSegment(segment: EditableSegment) {
  return ["buffer", "travel_buffer"].includes(segment.kind);
}

function defaultDayTitle(dayNumber: number): string {
  if (dayNumber === 1) {
    return "历史中轴线与老北京风情";
  }
  if (dayNumber === 2) {
    return "皇家园林与人文体验";
  }
  return "待规划日程";
}

function isValidClockTime(value: string): boolean {
  return /^([01]\d|2[0-3]):[0-5]\d$/.test(value);
}

function toMinutes(value: string): number {
  const [hours, minutes] = value.split(":").map(Number);
  return hours * 60 + minutes;
}

import type {
  ComparisonSummary,
  ConversationTurn,
  GuideEvidenceUsage,
  ItineraryPlan,
  ProposalSegmentVisitFacts
} from "../services/apiClient";

export type MapInteractionMode =
  | "itinerary_edit"
  | "plan_comparison_preview"
  | "plan_overview_preview"
  | "pending_slot_comparison";

export type MapInteractionCapabilities = {
  navigate: boolean;
  inspect: boolean;
  search: boolean;
  mutateItinerary: boolean;
  confirmPendingSlot: boolean;
};

export function mapInteractionCapabilities(mode: MapInteractionMode): MapInteractionCapabilities {
  return {
    navigate: true,
    inspect: true,
    search: mode === "itinerary_edit",
    mutateItinerary: mode === "itinerary_edit",
    confirmPendingSlot: mode === "pending_slot_comparison"
  };
}

export type ComparisonPendingSlot = {
  briefId?: string;
  poolId?: string;
  planningSlotId: string;
  dayNumber: number;
  timeWindow?: string | null;
  startTime?: string | null;
  endTime?: string | null;
  timeLabel?: string;
  timingStatus?: "awaiting_route_confirmation" | "schedule_conflict_pending" | "time_pending" | string;
  timingBasis?: string | null;
  constraintSummary?: string | null;
  placementAfterSegmentId?: string | null;
  placementBeforeSegmentId?: string | null;
  displayNeed: string;
  requirementLevel?: string;
};

export type ComparisonPlanProjection = {
  planningSelectionRootTurnId: string;
  rootPortfolioId: string;
  proposalId: string;
  sourceAssistantTurnId: string;
  choiceId: string;
  materialFingerprint?: string;
  repairChoiceId?: string;
  workflowMode?: "simple_direction_v1" | string;
  status: string;
  isPartial: boolean;
  isAdopted: boolean;
  adoptionReady: boolean;
  confirmationPassed?: boolean;
  requiredPlanningDayNumbers?: number[];
  explicitRestDayNumbers?: number[];
  uncoveredDayNumbers?: number[];
  draftAdoptionReady?: boolean;
  partialAdoptionReady?: boolean;
  strictlyVerified?: boolean;
  structureReady?: boolean;
  pendingHardSlotCount?: number;
  pendingSoftSlotCount?: number;
  adoptionMode?: "complete" | "editable_draft" | "editable_partial" | "preview_only" | "blocked";
  themeEligible?: boolean;
  visibilityMode?: "themed" | "neutral_skeleton" | "skeleton_preview_only";
  completionAction?: {
    kind: "portfolio_theme_completion";
    theme: string;
    label: string;
    choiceId: string;
  } | null;
  pendingRatio?: number;
  desiredDensityAnchorTargets?: Record<string, number>;
  groundedRouteAnchorTargets?: Record<string, number>;
  pendingFutureAnchorTargets?: Record<string, number>;
  activeVersionId: string | null;
  expectedBaseVersionId: string | null;
  title: string;
  days: ItineraryPlan["days"];
  pendingSlots: ComparisonPendingSlot[];
  routeEvidence: ItineraryPlan["routeOptions"];
  routeStatus?: string;
  routeExpectedLegCount?: number;
  routeVerifiedLegCount?: number;
  routeErrorLegCount?: number;
  routeRetryable?: boolean;
  routeProviderAttemptCount?: number;
  routeProviderCacheHitCount?: number;
  routePreconditionFailureReason?: string | null;
  routeEvidenceInvalidationReason?: string | null;
  detourCompliance?: "verified" | "pending" | "exceeded";
  routeAssignmentEvidence?: Record<string, unknown>;
  mealExperienceBriefs?: Array<Record<string, unknown>>;
  mealSemanticEvidence?: Array<Record<string, unknown>>;
  mealThemeSignature?: string[];
  mealQualityPassed?: boolean;
  mealDiversityPassed?: boolean;
  mealUnresolvedReasons?: string[];
  routeComfortEvidence?: Record<string, unknown>;
  guideEvidenceUsage?: GuideEvidenceUsage;
  openingFactsRefreshStatus?: "not_started" | "refreshing" | "completed" | "partial" | "failed" | string;
  visitFactsBySegment?: Record<string, ProposalSegmentVisitFacts>;
  verifiedScheduleConflicts?: Array<Record<string, unknown>>;
  budgetSummary: string;
  budgetTier?: string;
  budgetTierLabel?: string;
  budgetStatus?: string;
  budgetEvidenceCount?: number;
  unknownCostSegmentCount?: number;
  routeSummary: string;
  blockingReasons?: string[];
  blockingReasonLabels?: string[];
  comparisonRole?: "candidate_proposal" | "current_active_draft";
  originProjectionMode?: "partial_preview" | "full_proposal" | "current_active_draft";
  currentReadiness?: "map_ready" | "route_pending" | "route_ready" | "blocked";
  promotionStatus?: "not_promotable" | "promotable" | "promoted";
  nextAction?:
    | "complete_pending_slots"
    | "verify_routes"
    | "verify_routes_and_adopt"
    | "retry_route_verification"
    | "adopt"
    | "adopt_proposal"
    | "adopt_editable_draft"
    | "confirm_edit"
    | "continue_editing"
    | "continue_grounding_hard_slots"
    | "none";
  nextActionLabel?: string;
  tradeoffSummary: string;
  colorKey: string;
};

export function proposalVisitFactsNeedsAutoRefresh(plan: ComparisonPlanProjection): boolean {
  const status = String(plan.openingFactsRefreshStatus ?? "not_started").trim();
  if (!status || status === "not_started") return true;
  if (status === "refreshing") return false;
  const facts = Object.values(plan.visitFactsBySegment ?? {});
  // One proposal/material gets at most one automatic attempt per AppShell
  // mount. A failed request stays visible instead of creating an effect-driven
  // retry loop; a later remount can retry when the server projects not_started.
  if (!facts.length) return false;
  return facts.some((fact) => fact.refreshStatus === "expired");
}

export type ComparisonPlanReadiness = {
  confirmationReady: boolean;
  dayContractPresent: boolean;
  requiredDayCoverageComplete: boolean;
  routeCoverageComplete: boolean;
  mealEvidenceComplete: boolean;
  uncoveredDayNumbers: number[];
  expectedRoutePairCount: number;
};

export function comparisonPlanReadiness(plan: ComparisonPlanProjection): ComparisonPlanReadiness {
  const isSimpleOpen = plan.workflowMode === "simple_direction_v1";
  const hasBlockingReason = (plan.blockingReasons?.length ?? 0) > 0;
  if (!isSimpleOpen) {
    const confirmationAccepted = typeof plan.confirmationPassed === "boolean" ? plan.confirmationPassed : true;
    return {
      confirmationReady: plan.adoptionReady === true && confirmationAccepted && !hasBlockingReason,
      dayContractPresent: false,
      requiredDayCoverageComplete: true,
      routeCoverageComplete: true,
      mealEvidenceComplete: true,
      uncoveredDayNumbers: [],
      expectedRoutePairCount: 0
    };
  }

  const requiredDayNumbers = plan.requiredPlanningDayNumbers;
  const explicitRestDayNumbers = plan.explicitRestDayNumbers;
  const declaredUncoveredDayNumbers = plan.uncoveredDayNumbers;
  const dayContractPresent =
    Array.isArray(requiredDayNumbers) &&
    requiredDayNumbers.length > 0 &&
    Array.isArray(explicitRestDayNumbers) &&
    Array.isArray(declaredUncoveredDayNumbers);

  if (!dayContractPresent) {
    return {
      confirmationReady: false,
      dayContractPresent: false,
      requiredDayCoverageComplete: false,
      routeCoverageComplete: false,
      mealEvidenceComplete: false,
      uncoveredDayNumbers: [],
      expectedRoutePairCount: 0
    };
  }

  const required = requiredDayNumbers as number[];
  const rest = explicitRestDayNumbers as number[];
  const declaredUncovered = declaredUncoveredDayNumbers as number[];
  const requiredSet = new Set(required);
  const restSet = new Set(rest);
  const declaredDaySet = new Set([...required, ...rest]);
  const daysByNumber = new Map<number, ComparisonPlanProjection["days"][number]>();
  let duplicateDayNumber = false;
  for (const day of plan.days) {
    if (daysByNumber.has(day.dayNumber)) duplicateDayNumber = true;
    daysByNumber.set(day.dayNumber, day);
  }

  const dayRoleConflict = required.some((dayNumber) => restSet.has(dayNumber));
  const uncoveredRoleConflict = declaredUncovered.some((dayNumber) => !requiredSet.has(dayNumber));
  const unexpectedDay = plan.days.some((day) => !declaredDaySet.has(day.dayNumber));
  const missingRequiredDayNumbers = required.filter((dayNumber) => {
    const day = daysByNumber.get(dayNumber);
    return !day || day.segments.length === 0 || !day.segments.every(hasVerifiedAmapIdentity);
  });
  const invalidRestDay = rest.some((dayNumber) => {
    const day = daysByNumber.get(dayNumber);
    return !day || day.segments.length > 0;
  });
  const uncoveredDayNumbers = [...new Set([...declaredUncovered, ...missingRequiredDayNumbers])].sort(
    (left, right) => left - right
  );
  const requiredDayCoverageComplete =
    !duplicateDayNumber &&
    !dayRoleConflict &&
    !uncoveredRoleConflict &&
    !unexpectedDay &&
    !invalidRestDay &&
    uncoveredDayNumbers.length === 0;
  const expectedRoutePairCount = required.reduce((total, dayNumber) => {
    const stopCount = daysByNumber.get(dayNumber)?.segments.filter(isPhysicalRouteTargetSegment).length ?? 0;
    return total + Math.max(stopCount - 1, 0);
  }, 0);
  const declaredExpectedRoutePairCount = plan.routeExpectedLegCount;
  const declaredVerifiedRoutePairCount = plan.routeVerifiedLegCount;
  const routeCountMatches =
    declaredExpectedRoutePairCount === expectedRoutePairCount &&
    declaredVerifiedRoutePairCount === expectedRoutePairCount;
  const routeStateMatches =
    expectedRoutePairCount > 0
      ? plan.routeStatus === "route_ready"
      : ["route_ready", "route_not_required"].includes(plan.routeStatus ?? "");
  const routeCoverageComplete =
    routeCountMatches &&
    routeStateMatches &&
    (plan.routeErrorLegCount ?? 0) === 0 &&
    !plan.routePreconditionFailureReason &&
    !plan.routeEvidenceInvalidationReason;
  const hasOpaqueConfirmationCapability =
    plan.sourceAssistantTurnId.trim().length > 0 &&
    plan.choiceId.trim().length > 0 &&
    plan.nextAction === "confirm_edit";
  const mealSegmentCount = plan.days.reduce((total, day) => total + day.segments.filter(isMealSegment).length, 0);
  const mealEvidenceComplete =
    mealSegmentCount === 0 ||
    (plan.mealQualityPassed === true &&
      plan.mealDiversityPassed === true &&
      Array.isArray(plan.mealUnresolvedReasons) &&
      plan.mealUnresolvedReasons.length === 0 &&
      Array.isArray(plan.mealSemanticEvidence) &&
      plan.mealSemanticEvidence.length === mealSegmentCount);

  return {
    confirmationReady:
      plan.adoptionReady === true &&
      plan.confirmationPassed === true &&
      !hasBlockingReason &&
      requiredDayCoverageComplete &&
      routeCoverageComplete &&
      mealEvidenceComplete &&
      hasOpaqueConfirmationCapability,
    dayContractPresent,
    requiredDayCoverageComplete,
    routeCoverageComplete,
    mealEvidenceComplete,
    uncoveredDayNumbers,
    expectedRoutePairCount
  };
}

function isMealSegment(segment: ComparisonPlanProjection["days"][number]["segments"][number]): boolean {
  const metadata = isRecord(segment.semanticMetadata) ? segment.semanticMetadata : {};
  return segment.kind === "meal" || metadata.intentType === "meal";
}

function hasVerifiedAmapIdentity(segment: ComparisonPlanProjection["days"][number]["segments"][number]): boolean {
  const poi = segment.poi;
  return Boolean(
    poi.source === "amap-place-search" &&
    poi.amapId?.trim() &&
    typeof poi.latitude === "number" &&
    Number.isFinite(poi.latitude) &&
    poi.latitude !== 0 &&
    typeof poi.longitude === "number" &&
    Number.isFinite(poi.longitude) &&
    poi.longitude !== 0
  );
}

function isPhysicalRouteTargetSegment(segment: ComparisonPlanProjection["days"][number]["segments"][number]): boolean {
  return Boolean(
    hasVerifiedAmapIdentity(segment) &&
    !["pending", "placeholder"].includes(segment.kind) &&
    segment.semanticMetadata?.requiresRouteEdge !== false
  );
}

export type PlanComparisonPreviewState = {
  planningSelectionRootTurnId: string | null;
  rootPortfolioId: string | null;
  focusedProposalId: string | null;
  adoptedProposalId: string | null;
  adoptedVersionId: string | null;
  autoNavigationCompleted: boolean;
  autoNavigationCount: number;
  mapMode: MapInteractionMode;
  isMapReadOnly: boolean;
  comparisonSummary?: ComparisonSummary | null;
  plans: ComparisonPlanProjection[];
};

const PLAN_COLOR_KEYS = ["ocean", "amber", "violet", "teal", "rose", "indigo", "lime", "slate"] as const;

const PLAN_COLOR_HUES: Record<(typeof PLAN_COLOR_KEYS)[number], number> = {
  ocean: 235,
  amber: 80,
  violet: 305,
  teal: 175,
  rose: 15,
  indigo: 270,
  lime: 125,
  slate: 340
};

export function perceptualPlanColorDistance(left: string, right: string): number {
  const leftHue = PLAN_COLOR_HUES[left as (typeof PLAN_COLOR_KEYS)[number]];
  const rightHue = PLAN_COLOR_HUES[right as (typeof PLAN_COLOR_KEYS)[number]];
  if (leftHue === undefined || rightHue === undefined) return 0;
  const difference = Math.abs(leftHue - rightHue) % 360;
  return Math.min(difference, 360 - difference);
}

export function stablePlanColorKey(identity: string): string {
  let hash = 2166136261;
  for (let index = 0; index < identity.length; index += 1) {
    hash ^= identity.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return PLAN_COLOR_KEYS[Math.abs(hash >>> 0) % PLAN_COLOR_KEYS.length];
}

function availablePlanColorKey(identity: string, plans: ComparisonPlanProjection[]): string {
  const preferred = stablePlanColorKey(identity);
  const used = new Set(plans.map((plan) => plan.colorKey));
  const start = PLAN_COLOR_KEYS.indexOf(preferred as (typeof PLAN_COLOR_KEYS)[number]);
  const candidates = PLAN_COLOR_KEYS.filter((candidate) => !used.has(candidate));
  if (candidates.length === 0) return preferred;
  if (used.size === 0) return preferred;
  return candidates.reduce((best, candidate) => {
    const candidateDistance = Math.min(...[...used].map((color) => perceptualPlanColorDistance(candidate, color)));
    const bestDistance = Math.min(...[...used].map((color) => perceptualPlanColorDistance(best, color)));
    if (candidateDistance !== bestDistance) return candidateDistance > bestDistance ? candidate : best;
    const candidateOffset =
      (PLAN_COLOR_KEYS.indexOf(candidate) - start + PLAN_COLOR_KEYS.length) % PLAN_COLOR_KEYS.length;
    const bestOffset = (PLAN_COLOR_KEYS.indexOf(best) - start + PLAN_COLOR_KEYS.length) % PLAN_COLOR_KEYS.length;
    return candidateOffset < bestOffset ? candidate : best;
  }, candidates[0]);
}

export function createComparisonPreviewState(): PlanComparisonPreviewState {
  return {
    planningSelectionRootTurnId: null,
    rootPortfolioId: null,
    focusedProposalId: null,
    adoptedProposalId: null,
    adoptedVersionId: null,
    autoNavigationCompleted: false,
    autoNavigationCount: 0,
    mapMode: "itinerary_edit",
    isMapReadOnly: false,
    plans: []
  };
}

export function isCurrentComparisonScope(
  current: PlanComparisonPreviewState,
  identity: {
    planningSelectionRootTurnId?: string | null;
    rootPortfolioId?: string | null;
  }
): boolean {
  return Boolean(
    current.planningSelectionRootTurnId &&
    current.rootPortfolioId &&
    identity.planningSelectionRootTurnId === current.planningSelectionRootTurnId &&
    identity.rootPortfolioId === current.rootPortfolioId
  );
}

function preserveSameMaterialVisitFacts(
  existing: ComparisonPlanProjection | undefined,
  incoming: ComparisonPlanProjection
): ComparisonPlanProjection {
  if (!existing) return incoming;
  const materialFingerprint = incoming.materialFingerprint?.trim();
  const sameIdentity =
    existing.planningSelectionRootTurnId === incoming.planningSelectionRootTurnId &&
    existing.rootPortfolioId === incoming.rootPortfolioId &&
    existing.proposalId === incoming.proposalId;
  const sameMaterial = Boolean(
    materialFingerprint && existing.materialFingerprint?.trim() === materialFingerprint
  );
  const existingFacts = existing.visitFactsBySegment ?? {};
  const incomingFacts = incoming.visitFactsBySegment ?? {};
  const incomingStatus = String(incoming.openingFactsRefreshStatus ?? "not_started").trim() || "not_started";
  const staleEmptyCarrier = incomingStatus === "not_started" && Object.keys(incomingFacts).length === 0;
  if (!sameIdentity || !sameMaterial || !staleEmptyCarrier || Object.keys(existingFacts).length === 0) {
    return incoming;
  }
  return {
    ...incoming,
    openingFactsRefreshStatus: existing.openingFactsRefreshStatus,
    visitFactsBySegment: existingFacts,
    verifiedScheduleConflicts: existing.verifiedScheduleConflicts
  };
}

export function upsertVisibleComparisonPlan(
  current: PlanComparisonPreviewState,
  incoming: ComparisonPlanProjection
): { state: PlanComparisonPreviewState; shouldAutoNavigate: boolean } {
  const samePlanningRoot = current.planningSelectionRootTurnId === incoming.planningSelectionRootTurnId;
  if (samePlanningRoot && current.rootPortfolioId !== null && current.rootPortfolioId !== incoming.rootPortfolioId) {
    return { state: current, shouldAutoNavigate: false };
  }
  const sameRoot =
    samePlanningRoot && (current.rootPortfolioId === null || current.rootPortfolioId === incoming.rootPortfolioId);
  const scopeChanged = current.planningSelectionRootTurnId !== null && !sameRoot;
  // A new planning root supersedes the old root's capabilities, not its visible
  // evidence. Keep prior cards as comparison-only history and move the active
  // scope to the incoming root so stale actions cannot target it.
  const base = current;
  const existingIndex = base.plans.findIndex(
    (plan) =>
      plan.proposalId === incoming.proposalId &&
      plan.planningSelectionRootTurnId === incoming.planningSelectionRootTurnId &&
      plan.rootPortfolioId === incoming.rootPortfolioId
  );
  const evidenceSafeIncoming = preserveSameMaterialVisitFacts(
    existingIndex >= 0 ? base.plans[existingIndex] : undefined,
    incoming
  );
  const normalized = {
    ...evidenceSafeIncoming,
    colorKey:
      existingIndex >= 0 ? base.plans[existingIndex].colorKey : availablePlanColorKey(incoming.proposalId, base.plans),
    pendingSlots: sortPendingSlots(evidenceSafeIncoming.pendingSlots)
  };
  const plans = [...base.plans];
  if (existingIndex >= 0) {
    plans[existingIndex] = normalized;
  } else {
    plans.push(normalized);
  }
  const shouldAutoNavigate =
    plans.length > 0 &&
    !incoming.isAdopted &&
    (scopeChanged || (!base.autoNavigationCompleted && !base.adoptedProposalId));
  const stateBase = scopeChanged ? withoutComparisonSummary(base) : base;
  return {
    shouldAutoNavigate,
    state: {
      ...stateBase,
      planningSelectionRootTurnId: incoming.planningSelectionRootTurnId,
      rootPortfolioId: incoming.rootPortfolioId,
      plans,
      focusedProposalId: scopeChanged ? incoming.proposalId : (base.focusedProposalId ?? incoming.proposalId),
      autoNavigationCompleted: scopeChanged ? shouldAutoNavigate : base.autoNavigationCompleted || shouldAutoNavigate,
      autoNavigationCount: base.autoNavigationCount + (shouldAutoNavigate ? 1 : 0),
      mapMode: shouldAutoNavigate ? "plan_comparison_preview" : base.mapMode,
      isMapReadOnly: shouldAutoNavigate ? true : base.isMapReadOnly
    }
  };
}

export function focusComparisonPlan(
  current: PlanComparisonPreviewState,
  proposalId: string
): PlanComparisonPreviewState {
  const selected = current.plans.find((plan) => plan.proposalId === proposalId);
  if (!selected) return current;
  return {
    ...current,
    focusedProposalId: proposalId,
    mapMode: "plan_overview_preview",
    isMapReadOnly: true
  };
}

export function beginPendingSlotComparison(
  current: PlanComparisonPreviewState,
  proposalId: string
): PlanComparisonPreviewState {
  const selected = current.plans.find((plan) => plan.proposalId === proposalId);
  if (!selected || !isCurrentComparisonScope(current, selected)) return current;
  return {
    ...current,
    focusedProposalId: proposalId,
    mapMode: "pending_slot_comparison",
    isMapReadOnly: true
  };
}

export function markComparisonPlanAdopted(
  current: PlanComparisonPreviewState,
  proposalId: string,
  versionId: string
): PlanComparisonPreviewState {
  const selected = current.plans.find((plan) => plan.proposalId === proposalId);
  if (!selected || !isCurrentComparisonScope(current, selected)) {
    return current;
  }
  return {
    ...current,
    focusedProposalId: proposalId,
    adoptedProposalId: proposalId,
    adoptedVersionId: versionId,
    mapMode: "itinerary_edit",
    isMapReadOnly: false,
    plans: current.plans.map((plan) => ({
      ...plan,
      isAdopted: plan.proposalId === proposalId,
      activeVersionId: plan.proposalId === proposalId ? versionId : plan.activeVersionId
    }))
  };
}

export function updateComparisonPlanSnapshot(
  current: PlanComparisonPreviewState,
  incoming: ComparisonPlanProjection
): PlanComparisonPreviewState {
  const existingIndex = current.plans.findIndex(
    (plan) =>
      plan.proposalId === incoming.proposalId &&
      plan.planningSelectionRootTurnId === incoming.planningSelectionRootTurnId &&
      plan.rootPortfolioId === incoming.rootPortfolioId
  );
  if (existingIndex < 0) return current;
  const plans = [...current.plans];
  const evidenceSafeIncoming = preserveSameMaterialVisitFacts(plans[existingIndex], incoming);
  plans[existingIndex] = {
    ...evidenceSafeIncoming,
    colorKey: plans[existingIndex].colorKey,
    pendingSlots: sortPendingSlots(evidenceSafeIncoming.pendingSlots)
  };
  return {
    ...current,
    adoptedProposalId: incoming.isAdopted ? incoming.proposalId : current.adoptedProposalId,
    adoptedVersionId:
      incoming.isAdopted || current.adoptedProposalId === incoming.proposalId
        ? (incoming.activeVersionId ?? current.adoptedVersionId)
        : current.adoptedVersionId,
    plans
  };
}

export function sortPendingSlots<T extends ComparisonPendingSlot>(slots: T[]): T[] {
  return [...slots].sort((left, right) => {
    if (left.dayNumber !== right.dayNumber) return left.dayNumber - right.dayNumber;
    const leftTime = authoritativeTime(left);
    const rightTime = authoritativeTime(right);
    if (leftTime === rightTime) return left.planningSlotId.localeCompare(right.planningSlotId);
    if (leftTime === Number.POSITIVE_INFINITY) return 1;
    if (rightTime === Number.POSITIVE_INFINITY) return -1;
    return leftTime - rightTime;
  });
}

function authoritativeTime(slot: ComparisonPendingSlot): number {
  const raw = slot.startTime || slot.timeWindow?.match(/\b\d{1,2}:\d{2}\b/)?.[0] || "";
  const match = raw.match(/^(\d{1,2}):(\d{2})$/);
  return match ? Number(match[1]) * 60 + Number(match[2]) : Number.POSITIVE_INFINITY;
}

function guideEvidenceUsageFromUnknown(value: unknown): GuideEvidenceUsage | undefined {
  if (!isRecord(value)) return undefined;
  if (
    typeof value.schemaVersion !== "string" ||
    !value.schemaVersion.trim() ||
    typeof value.status !== "string" ||
    !value.status.trim() ||
    typeof value.evidenceFingerprint !== "string" ||
    !value.evidenceFingerprint.trim() ||
    !Number.isInteger(value.requiredMinimum) ||
    Number(value.requiredMinimum) < 0 ||
    !Array.isArray(value.usedPlaces) ||
    !isRecord(value.rejectionCounts)
  ) {
    return undefined;
  }
  const usedPlaces = value.usedPlaces.map((item) => {
    if (!isRecord(item)) return null;
    const sourceRefIds = stringArray(item.sourceRefIds);
    if (
      !requiredString(item.mentionText) ||
      !requiredString(item.intentType) ||
      sourceRefIds.length === 0 ||
      !requiredString(item.amapPoiId) ||
      !requiredString(item.physicalIdentityKey) ||
      !Number.isInteger(item.dayNumber) ||
      Number(item.dayNumber) < 1 ||
      !requiredString(item.planningSlotId) ||
      typeof item.routeVerified !== "boolean"
    ) {
      return null;
    }
    return {
      mentionText: String(item.mentionText).trim(),
      intentType: String(item.intentType).trim(),
      sourceRefIds,
      amapPoiId: String(item.amapPoiId).trim(),
      physicalIdentityKey: String(item.physicalIdentityKey).trim(),
      dayNumber: Number(item.dayNumber),
      planningSlotId: String(item.planningSlotId).trim(),
      routeVerified: item.routeVerified
    };
  });
  if (usedPlaces.some((item) => item === null)) return undefined;
  const rejectionCounts = Object.fromEntries(
    Object.entries(value.rejectionCounts).filter(
      (entry): entry is [string, number] =>
        typeof entry[0] === "string" &&
        Boolean(entry[0].trim()) &&
        typeof entry[1] === "number" &&
        Number.isInteger(entry[1]) &&
        entry[1] >= 0
    )
  );
  return {
    schemaVersion: value.schemaVersion,
    status: value.status,
    evidenceFingerprint: value.evidenceFingerprint,
    requiredMinimum: Number(value.requiredMinimum),
    usedPlaces: usedPlaces as GuideEvidenceUsage["usedPlaces"],
    rejectionCounts
  };
}

export function comparisonProjectionFromUnknown(value: unknown): ComparisonPlanProjection | null {
  if (!value || typeof value !== "object") return null;
  const record = value as Record<string, unknown>;
  const planningSelectionRootTurnId = requiredString(record.planningSelectionRootTurnId);
  const rootPortfolioId = requiredString(record.rootPortfolioId);
  const proposalId = requiredString(record.proposalId);
  const sourceAssistantTurnId = requiredString(record.sourceAssistantTurnId);
  const choiceId = requiredString(record.choiceId);
  if (!planningSelectionRootTurnId || !rootPortfolioId || !proposalId || !sourceAssistantTurnId || !choiceId) {
    return null;
  }
  if (typeof record.isPartial !== "boolean" || typeof record.adoptionReady !== "boolean") {
    return null;
  }
  if (record.confirmationPassed !== undefined && typeof record.confirmationPassed !== "boolean") {
    return null;
  }
  if (
    (record.mealQualityPassed !== undefined && typeof record.mealQualityPassed !== "boolean") ||
    (record.mealDiversityPassed !== undefined && typeof record.mealDiversityPassed !== "boolean") ||
    (record.mealUnresolvedReasons !== undefined && !isStringArray(record.mealUnresolvedReasons)) ||
    (record.mealThemeSignature !== undefined && !isStringArray(record.mealThemeSignature)) ||
    (record.mealExperienceBriefs !== undefined && !isRecordArray(record.mealExperienceBriefs)) ||
    (record.mealSemanticEvidence !== undefined && !isRecordArray(record.mealSemanticEvidence)) ||
    (record.routeComfortEvidence !== undefined && !isRecord(record.routeComfortEvidence))
  ) {
    return null;
  }
  const requiredPlanningDayNumbers = optionalDayNumberArray(record.requiredPlanningDayNumbers);
  const explicitRestDayNumbers = optionalDayNumberArray(record.explicitRestDayNumbers);
  const uncoveredDayNumbers = optionalDayNumberArray(record.uncoveredDayNumbers);
  if (requiredPlanningDayNumbers === null || explicitRestDayNumbers === null || uncoveredDayNumbers === null) {
    return null;
  }
  const pendingSlots = Array.isArray(record.pendingSlots)
    ? record.pendingSlots.map(comparisonPendingSlotFromUnknown)
    : [];
  if (pendingSlots.some((slot) => slot === null)) return null;
  if (
    (record.days !== undefined && !Array.isArray(record.days)) ||
    (record.routeEvidence !== undefined && !Array.isArray(record.routeEvidence))
  ) {
    return null;
  }
  const days =
    Array.isArray(record.days) && record.days.every(isComparisonDay) ? (record.days as ItineraryPlan["days"]) : [];
  const routeEvidence =
    Array.isArray(record.routeEvidence) && record.routeEvidence.every(isComparisonRoute)
      ? (record.routeEvidence as ItineraryPlan["routeOptions"])
      : [];
  if (
    (Array.isArray(record.days) && record.days.length !== days.length) ||
    (Array.isArray(record.routeEvidence) && record.routeEvidence.length !== routeEvidence.length)
  ) {
    return null;
  }
  const rawCompletionAction = record.completionAction;
  const completionAction =
    rawCompletionAction == null
      ? null
      : isRecord(rawCompletionAction) &&
          rawCompletionAction.kind === "portfolio_theme_completion" &&
          Boolean(requiredString(rawCompletionAction.theme)) &&
          Boolean(requiredString(rawCompletionAction.label)) &&
          Boolean(requiredString(rawCompletionAction.choiceId))
        ? {
            kind: "portfolio_theme_completion" as const,
            theme: String(rawCompletionAction.theme),
            label: String(rawCompletionAction.label),
            choiceId: String(rawCompletionAction.choiceId)
          }
        : undefined;
  if (rawCompletionAction != null && completionAction === undefined) return null;
  const adoptionMode =
    record.adoptionMode === "complete" ||
    record.adoptionMode === "editable_draft" ||
    record.adoptionMode === "editable_partial" ||
    record.adoptionMode === "preview_only"
      ? record.adoptionMode
      : "blocked";
  return {
    planningSelectionRootTurnId,
    rootPortfolioId,
    proposalId,
    sourceAssistantTurnId,
    choiceId,
    materialFingerprint: optionalString(record.materialFingerprint) ?? undefined,
    repairChoiceId: optionalString(record.repairChoiceId) ?? undefined,
    workflowMode: optionalString(record.workflowMode) ?? undefined,
    status: stringOr(record.status, "complete"),
    isPartial: record.isPartial,
    isAdopted: record.isAdopted === true,
    adoptionReady: record.adoptionReady,
    confirmationPassed: typeof record.confirmationPassed === "boolean" ? record.confirmationPassed : undefined,
    requiredPlanningDayNumbers,
    explicitRestDayNumbers,
    uncoveredDayNumbers,
    draftAdoptionReady: record.draftAdoptionReady === true,
    strictlyVerified: record.strictlyVerified === true,
    structureReady: record.structureReady === true,
    pendingHardSlotCount: integerOr(record.pendingHardSlotCount, 0),
    pendingSoftSlotCount: integerOr(record.pendingSoftSlotCount, 0),
    adoptionMode,
    partialAdoptionReady: record.partialAdoptionReady === true,
    themeEligible: typeof record.themeEligible === "boolean" ? record.themeEligible : undefined,
    visibilityMode:
      record.visibilityMode === "themed" ||
      record.visibilityMode === "neutral_skeleton" ||
      record.visibilityMode === "skeleton_preview_only"
        ? record.visibilityMode
        : undefined,
    completionAction,
    pendingRatio:
      typeof record.pendingRatio === "number" && Number.isFinite(record.pendingRatio) ? record.pendingRatio : undefined,
    desiredDensityAnchorTargets: integerRecord(record.desiredDensityAnchorTargets),
    groundedRouteAnchorTargets: integerRecord(record.groundedRouteAnchorTargets),
    pendingFutureAnchorTargets: integerRecord(record.pendingFutureAnchorTargets),
    openingFactsRefreshStatus: optionalString(record.refreshStatus) ?? "not_started",
    visitFactsBySegment: isRecord(record.visitFactsBySegment)
      ? (record.visitFactsBySegment as Record<string, ProposalSegmentVisitFacts>)
      : {},
    verifiedScheduleConflicts: Array.isArray(record.verifiedScheduleConflicts)
      ? record.verifiedScheduleConflicts.filter(isRecord)
      : [],
    activeVersionId: nullableString(record.activeVersionId),
    expectedBaseVersionId: nullableString(record.expectedBaseVersionId),
    title: stringOr(record.title, "行程方案"),
    days,
    pendingSlots: pendingSlots as ComparisonPendingSlot[],
    routeEvidence,
    routeStatus: stringOr(record.routeStatus, "route_pending"),
    routeExpectedLegCount: integerOr(record.routeExpectedLegCount, 0),
    routeVerifiedLegCount: integerOr(record.routeVerifiedLegCount, 0),
    routeErrorLegCount: integerOr(record.routeErrorLegCount, 0),
    routeRetryable: record.routeRetryable === true,
    routeProviderAttemptCount: integerOr(record.routeProviderAttemptCount, 0),
    routeProviderCacheHitCount: integerOr(record.routeProviderCacheHitCount, 0),
    routePreconditionFailureReason: optionalString(record.routePreconditionFailureReason),
    routeEvidenceInvalidationReason: optionalString(record.routeEvidenceInvalidationReason),
    detourCompliance:
      record.detourCompliance === "verified" || record.detourCompliance === "exceeded"
        ? record.detourCompliance
        : "pending",
    routeAssignmentEvidence: isRecord(record.routeAssignmentEvidence) ? record.routeAssignmentEvidence : undefined,
    mealExperienceBriefs: isRecordArray(record.mealExperienceBriefs) ? record.mealExperienceBriefs : undefined,
    mealSemanticEvidence: isRecordArray(record.mealSemanticEvidence) ? record.mealSemanticEvidence : undefined,
    mealThemeSignature: isStringArray(record.mealThemeSignature) ? record.mealThemeSignature : undefined,
    mealQualityPassed: typeof record.mealQualityPassed === "boolean" ? record.mealQualityPassed : undefined,
    mealDiversityPassed: typeof record.mealDiversityPassed === "boolean" ? record.mealDiversityPassed : undefined,
    mealUnresolvedReasons: isStringArray(record.mealUnresolvedReasons) ? record.mealUnresolvedReasons : undefined,
    routeComfortEvidence: isRecord(record.routeComfortEvidence) ? record.routeComfortEvidence : undefined,
    guideEvidenceUsage: guideEvidenceUsageFromUnknown(record.guideEvidenceUsage),
    budgetSummary: localizedBudgetSummary(record),
    budgetTier: stringOr(record.budgetTier, "unknown"),
    budgetTierLabel: stringOr(record.budgetTierLabel, "预算档位待确认"),
    budgetStatus: stringOr(record.budgetStatus, "pending"),
    budgetEvidenceCount: integerOr(record.budgetEvidenceCount, 0),
    unknownCostSegmentCount: integerOr(record.unknownCostSegmentCount, 0),
    routeSummary: stringOr(record.routeSummary, "路线待核验"),
    blockingReasons: stringArray(record.blockingReasons),
    blockingReasonLabels: stringArray(record.blockingReasonLabels),
    comparisonRole: record.comparisonRole === "current_active_draft" ? "current_active_draft" : "candidate_proposal",
    originProjectionMode:
      record.originProjectionMode === "partial_preview" || record.originProjectionMode === "current_active_draft"
        ? record.originProjectionMode
        : "full_proposal",
    currentReadiness:
      record.currentReadiness === "map_ready" ||
      record.currentReadiness === "route_ready" ||
      record.currentReadiness === "blocked"
        ? record.currentReadiness
        : "route_pending",
    promotionStatus:
      record.promotionStatus === "promoted" || record.promotionStatus === "promotable"
        ? record.promotionStatus
        : "not_promotable",
    nextAction: comparisonNextAction(record.nextAction),
    nextActionLabel: stringOr(record.nextActionLabel, ""),
    tradeoffSummary: stringOr(record.tradeoffSummary, ""),
    colorKey: stringOr(record.colorKey, stablePlanColorKey(proposalId))
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function integerRecord(value: unknown): Record<string, number> | undefined {
  if (value === undefined) return undefined;
  if (!isRecord(value)) return undefined;
  const entries = Object.entries(value);
  if (entries.some(([, item]) => !Number.isInteger(item) || Number(item) < 0)) return undefined;
  return Object.fromEntries(entries.map(([key, item]) => [key, Number(item)]));
}

function isComparisonDay(value: unknown): boolean {
  if (!isRecord(value) || !Number.isInteger(value.dayNumber) || !Array.isArray(value.segments)) return false;
  return value.segments.every((segment) => {
    if (!isRecord(segment) || !requiredString(segment.id) || !isRecord(segment.poi)) return false;
    const poi = segment.poi;
    return Boolean(
      requiredString(poi.id) &&
      requiredString(poi.name) &&
      requiredString(poi.source) &&
      (poi.latitude === null || typeof poi.latitude === "number") &&
      (poi.longitude === null || typeof poi.longitude === "number")
    );
  });
}

function isComparisonRoute(value: unknown): boolean {
  if (!isRecord(value)) return false;
  return Boolean(
    requiredString(value.id) &&
    requiredString(value.fromSegmentId) &&
    requiredString(value.toSegmentId) &&
    requiredString(value.fromPoiId) &&
    requiredString(value.toPoiId) &&
    requiredString(value.provider) &&
    requiredString(value.mode) &&
    requiredString(value.label) &&
    typeof value.isSelected === "boolean" &&
    Number.isInteger(value.sortOrder) &&
    requiredString(value.transportMode) &&
    typeof value.distanceMeters === "number" &&
    typeof value.durationSeconds === "number" &&
    typeof value.durationMinutes === "number" &&
    typeof value.costAmount === "number" &&
    requiredString(value.costCurrency) &&
    typeof value.costEstimate === "number" &&
    typeof value.crowdingRisk === "string" &&
    requiredString(value.source) &&
    Array.isArray(value.polyline) &&
    Array.isArray(value.steps) &&
    isRecord(value.providerPayload) &&
    requiredString(value.queriedAt) &&
    requiredString(value.status)
  );
}

function comparisonNextAction(value: unknown): ComparisonPlanProjection["nextAction"] {
  return [
    "complete_pending_slots",
    "verify_routes",
    "verify_routes_and_adopt",
    "retry_route_verification",
    "adopt",
    "adopt_proposal",
    "adopt_editable_draft",
    "confirm_edit",
    "continue_grounding_hard_slots",
    "continue_editing",
    "none"
  ].includes(String(value))
    ? (value as ComparisonPlanProjection["nextAction"])
    : "none";
}

function localizedBudgetSummary(record: Record<string, unknown>): string {
  const explicitLabel =
    typeof record.budgetTierLabel === "string" && record.budgetTierLabel.trim() ? record.budgetTierLabel.trim() : "";
  const raw = stringOr(record.budgetSummary, "预算档位待确认 · 预算待核验");
  const tier = typeof record.budgetTier === "string" ? record.budgetTier.toLowerCase() : "";
  const label =
    explicitLabel || ({ low: "低预算", medium: "中等预算", high: "较高预算" } as Record<string, string>)[tier];
  const legacy = raw.match(/^(low|medium|high|unknown)(\s*·.*)?$/i);
  if (legacy) {
    const legacyLabel =
      ({ low: "低预算", medium: "中等预算", high: "较高预算" } as Record<string, string>)[legacy[1].toLowerCase()] ??
      "预算档位待确认";
    return `${legacyLabel}${legacy[2] ?? " · 预算待核验"}`;
  }
  if (label && !raw.startsWith(label)) {
    const suffix = raw.includes(" · ") ? raw.slice(raw.indexOf(" · ")) : ` · ${raw}`;
    return `${label}${suffix}`;
  }
  return raw;
}
function requiredString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function stringOr(value: unknown, fallback: string): string {
  return typeof value === "string" ? value : fallback;
}

function nullableString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function integerOr(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isInteger(value) && value >= 0 ? value : fallback;
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function optionalString(value: unknown): string | null | undefined {
  if (value === undefined) return undefined;
  if (value === null) return null;
  return typeof value === "string" ? value : undefined;
}

function comparisonPendingSlotFromUnknown(value: unknown): ComparisonPendingSlot | null {
  if (!isRecord(value)) return null;
  if (
    typeof value.planningSlotId !== "string" ||
    !value.planningSlotId.trim() ||
    !Number.isInteger(value.dayNumber) ||
    Number(value.dayNumber) < 1 ||
    typeof value.displayNeed !== "string" ||
    !value.displayNeed.trim()
  ) {
    return null;
  }
  const optionalFields = [
    value.briefId,
    value.poolId,
    value.timeWindow,
    value.startTime,
    value.endTime,
    value.timeLabel,
    value.timingStatus,
    value.timingBasis,
    value.constraintSummary,
    value.placementAfterSegmentId,
    value.placementBeforeSegmentId
  ];
  if (optionalFields.some((field) => field !== undefined && field !== null && typeof field !== "string")) {
    return null;
  }
  return {
    briefId: optionalString(value.briefId) ?? undefined,
    poolId: optionalString(value.poolId) ?? undefined,
    planningSlotId: value.planningSlotId.trim(),
    dayNumber: Number(value.dayNumber),
    timeWindow: optionalString(value.timeWindow),
    startTime: optionalString(value.startTime),
    endTime: optionalString(value.endTime),
    timeLabel: optionalString(value.timeLabel) ?? undefined,
    timingStatus: optionalString(value.timingStatus) ?? undefined,
    timingBasis: optionalString(value.timingBasis),
    constraintSummary: optionalString(value.constraintSummary),
    placementAfterSegmentId: optionalString(value.placementAfterSegmentId),
    placementBeforeSegmentId: optionalString(value.placementBeforeSegmentId),
    displayNeed: value.displayNeed.trim()
  };
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}

function isRecordArray(value: unknown): value is Array<Record<string, unknown>> {
  return Array.isArray(value) && value.every(isRecord);
}

function optionalDayNumberArray(value: unknown): number[] | undefined | null {
  if (value === undefined) return undefined;
  if (!Array.isArray(value)) return null;
  if (value.some((item) => !Number.isInteger(item) || Number(item) <= 0)) return null;
  const normalized = value.map(Number);
  if (new Set(normalized).size !== normalized.length) return null;
  return normalized.sort((left, right) => left - right);
}

export function comparisonSummaryFromUnknown(value: unknown): ComparisonSummary | null {
  if (!isRecord(value)) return null;
  const adoptionReadyCount = nonNegativeInteger(value.adoptionReadyCount);
  const repairablePartialCount = nonNegativeInteger(value.repairablePartialCount);
  const remainingQualifiedEntityCount = nonNegativeInteger(value.remainingQualifiedEntityCount);
  const frontierStatus = String(value.frontierStatus ?? "");
  if (
    adoptionReadyCount === null ||
    repairablePartialCount === null ||
    remainingQualifiedEntityCount === null ||
    !["has_more", "provider_pending", "qualification_exhausted", "poi_exhausted", "route_feasible_exhausted"].includes(
      frontierStatus
    )
  ) {
    return null;
  }
  const optionalCounts = [value.exploredQualifiedEntityCount, value.attemptedPoiPageCount, value.remainingPoiPageCount];
  if (optionalCounts.some((item) => item !== undefined && item !== null && nonNegativeInteger(item) === null)) {
    return null;
  }
  const optionalText = [
    value.lastOutcomeReason,
    value.blockingLayer,
    value.constraintModificationChoiceId,
    value.constraintModificationLabel
  ];
  if (optionalText.some((item) => item !== undefined && item !== null && typeof item !== "string")) {
    return null;
  }
  return {
    adoptionReadyCount,
    repairablePartialCount,
    remainingQualifiedEntityCount,
    remainingPoiPageCount: value.remainingPoiPageCount == null ? null : nonNegativeInteger(value.remainingPoiPageCount),
    frontierStatus: frontierStatus as ComparisonSummary["frontierStatus"],
    lastOutcomeReason: optionalString(value.lastOutcomeReason),
    exploredQualifiedEntityCount:
      value.exploredQualifiedEntityCount == null ? null : nonNegativeInteger(value.exploredQualifiedEntityCount),
    attemptedPoiPageCount: value.attemptedPoiPageCount == null ? null : nonNegativeInteger(value.attemptedPoiPageCount),
    blockingLayer: optionalString(value.blockingLayer),
    constraintModificationChoiceId: optionalString(value.constraintModificationChoiceId),
    constraintModificationLabel: optionalString(value.constraintModificationLabel)
  };
}

function nonNegativeInteger(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) && value >= 0 ? value : null;
}

export function comparisonPreviewFromTurns(
  turns: ConversationTurn[],
  current: PlanComparisonPreviewState = createComparisonPreviewState()
): PlanComparisonPreviewState {
  let state = createComparisonPreviewState();
  const projectionsByChoice = new Map<string, ComparisonPlanProjection>();
  const finalProposalIdsByScope = new Map<string, Set<string>>();
  for (const turn of turns) {
    if (!isComparisonProjectionCarrier(turn)) continue;
    const updateMode = comparisonProjectionUpdateMode(turn);
    if (updateMode === null) continue;
    const turnProposalIdsByScope = new Map<string, Set<string>>();
    const recordVisibleProposalId = (projection: ComparisonPlanProjection) => {
      const scopeKey = comparisonProjectionScopeKey(projection);
      const accumulated = finalProposalIdsByScope.get(scopeKey) ?? new Set<string>();
      accumulated.add(projection.proposalId);
      finalProposalIdsByScope.set(scopeKey, accumulated);
      const turnProposalIds = turnProposalIdsByScope.get(scopeKey) ?? new Set<string>();
      turnProposalIds.add(projection.proposalId);
      turnProposalIdsByScope.set(scopeKey, turnProposalIds);
    };
    for (const option of turn.choiceOptions ?? []) {
      const projection = comparisonProjectionFromUnknown(option.comparisonProjection);
      if (!projection || String(option.id ?? "") !== projection.choiceId) continue;
      recordVisibleProposalId(projection);
    }
    // A Simple direction projection is also authoritative comparison evidence
    // when its activation verifier blocks adoption and therefore the server
    // intentionally emits no executable choice. Recording only its proposal
    // identity keeps the card visible across append/replace replay; capability
    // execution remains exclusively bound below through choiceOptions.
    for (const candidate of Array.isArray(turn.comparisonProjections) ? turn.comparisonProjections : []) {
      const projection = comparisonProjectionFromUnknown(candidate);
      if (projection?.workflowMode === "simple_direction_v1") {
        recordVisibleProposalId(projection);
      }
    }
    if (updateMode === "replace") {
      const authoritativeScopes = new Set<string>();
      for (const candidate of Array.isArray(turn.comparisonProjections) ? turn.comparisonProjections : []) {
        const projection = comparisonProjectionFromUnknown(candidate);
        if (projection) {
          authoritativeScopes.add(comparisonProjectionScopeKey(projection));
        }
      }
      for (const scopeKey of authoritativeScopes) {
        finalProposalIdsByScope.set(scopeKey, turnProposalIdsByScope.get(scopeKey) ?? new Set<string>());
      }
    }
  }
  for (const turn of turns) {
    if (!isComparisonProjectionCarrier(turn)) continue;
    const updateMode = comparisonProjectionUpdateMode(turn);
    if (updateMode === null) continue;
    const immutableProposalKeys = new Set(
      state.plans.map(
        (plan) => `${plan.planningSelectionRootTurnId}\u0000${plan.rootPortfolioId}\u0000${plan.proposalId}`
      )
    );
    const applyProjection = (projection: ComparisonPlanProjection) => {
      const proposalKey = `${projection.planningSelectionRootTurnId}\u0000${projection.rootPortfolioId}\u0000${projection.proposalId}`;
      // Append is additive evidence only. Existing cards are refreshed solely
      // by a later authoritative replace carrier for the same portfolio scope.
      if (updateMode === "append" && immutableProposalKeys.has(proposalKey)) return;
      state = upsertVisibleComparisonPlan(state, projection).state;
    };
    for (const candidate of Array.isArray(turn.comparisonProjections) ? turn.comparisonProjections : []) {
      const projection = comparisonProjectionFromUnknown(candidate);
      const finalProposalIds = projection
        ? finalProposalIdsByScope.get(comparisonProjectionScopeKey(projection))
        : undefined;
      if (
        projection &&
        (Boolean(finalProposalIds?.has(projection.proposalId)) ||
          isTraceQualifiedReadOnlyPartialProjection(turn, projection))
      ) {
        applyProjection(projection);
      }
    }
    for (const event of turn.planningSteps ?? []) {
      const projection = comparisonProjectionFromUnknown(event.metadata?.comparisonProjection);
      const finalProposalIds = projection
        ? finalProposalIdsByScope.get(comparisonProjectionScopeKey(projection))
        : undefined;
      if (
        projection &&
        (Boolean(finalProposalIds?.has(projection.proposalId)) ||
          isTraceQualifiedReadOnlyPartialProjection(turn, projection))
      ) {
        applyProjection(projection);
      }
    }
    for (const option of turn.choiceOptions ?? []) {
      const projection = comparisonProjectionFromUnknown(option.comparisonProjection);
      if (!projection || String(option.id ?? "") !== projection.choiceId) continue;
      const finalProposalIds = finalProposalIdsByScope.get(comparisonProjectionScopeKey(projection));
      if (!finalProposalIds?.has(projection.proposalId)) {
        continue;
      }
      projectionsByChoice.set(`${turn.id}:${String(option.id ?? "")}`, projection);
      applyProjection(projection);
    }
  }
  for (const turn of turns) {
    if (!isComparisonProjectionCarrier(turn)) continue;
    const summary = comparisonSummaryFromUnknown(turn.comparisonSummary);
    const scope = comparisonScopeFromTurn(turn);
    if (
      summary &&
      scope?.planningSelectionRootTurnId === state.planningSelectionRootTurnId &&
      scope.rootPortfolioId === state.rootPortfolioId
    ) {
      state = { ...state, comparisonSummary: summary };
    }
  }
  const serverSelectedSimpleDirection = [...state.plans]
    .reverse()
    .find((plan) => plan.workflowMode === "simple_direction_v1" && plan.isAdopted && Boolean(plan.activeVersionId));
  if (serverSelectedSimpleDirection?.activeVersionId) {
    state = markComparisonPlanAdopted(
      state,
      serverSelectedSimpleDirection.proposalId,
      serverSelectedSimpleDirection.activeVersionId
    );
  }
  for (const turn of turns) {
    const trace = turn.structuredChoiceTrace;
    if ((turn.role !== "assistant" && turn.role !== "user") || turn.status !== "active") continue;
    if (trace?.executionStatus !== "succeeded") continue;
    const key = `${String(trace.sourceAssistantTurnId ?? "")}:${String(trace.resolvedChoiceId ?? "")}`;
    const projection = projectionsByChoice.get(key);
    const resultVersionId = String(trace.resultVersionId ?? turn.itineraryVersionId ?? "");
    if (
      projection &&
      resultVersionId &&
      projection.planningSelectionRootTurnId === state.planningSelectionRootTurnId &&
      projection.rootPortfolioId === state.rootPortfolioId &&
      state.plans.some(
        (plan) => plan.proposalId === projection.proposalId && plan.rootPortfolioId === projection.rootPortfolioId
      )
    ) {
      state = markComparisonPlanAdopted(state, projection.proposalId, resultVersionId);
    }
  }
  if (current.planningSelectionRootTurnId === state.planningSelectionRootTurnId && state.planningSelectionRootTurnId) {
    const currentPlansByIdentity = new Map(
      current.plans.map((plan) => [
        `${plan.planningSelectionRootTurnId}\u0000${plan.rootPortfolioId}\u0000${plan.proposalId}`,
        plan
      ])
    );
    state = {
      ...state,
      plans: state.plans.map((plan) =>
        preserveSameMaterialVisitFacts(
          currentPlansByIdentity.get(
            `${plan.planningSelectionRootTurnId}\u0000${plan.rootPortfolioId}\u0000${plan.proposalId}`
          ),
          plan
        )
      )
    };
    const focusedProposalId = state.plans.some((plan) => plan.proposalId === current.focusedProposalId)
      ? current.focusedProposalId
      : state.focusedProposalId;
    const adopted = Boolean(state.adoptedProposalId);
    const restoredSummary = state.comparisonSummary ?? current.comparisonSummary;
    state = {
      ...state,
      focusedProposalId,
      ...(restoredSummary ? { comparisonSummary: restoredSummary } : {}),
      autoNavigationCompleted: current.autoNavigationCompleted || state.autoNavigationCompleted,
      autoNavigationCount: Math.max(current.autoNavigationCount, state.autoNavigationCount),
      mapMode: adopted ? "itinerary_edit" : current.mapMode,
      isMapReadOnly: adopted ? false : current.isMapReadOnly
    };
  }
  return state;
}

function withoutComparisonSummary(state: PlanComparisonPreviewState): PlanComparisonPreviewState {
  const next = { ...state };
  delete next.comparisonSummary;
  return next;
}

function comparisonScopeFromTurn(
  turn: ConversationTurn
): { planningSelectionRootTurnId: string; rootPortfolioId: string } | null {
  const trace = turn.structuredChoiceTrace;
  const traceRoot = requiredString(trace?.planningSelectionRootTurnId);
  const tracePortfolio = requiredString(trace?.rootPortfolioId);
  if (traceRoot && tracePortfolio) {
    return { planningSelectionRootTurnId: traceRoot, rootPortfolioId: tracePortfolio };
  }
  const candidates: unknown[] = [
    ...(Array.isArray(turn.comparisonProjections) ? turn.comparisonProjections : []),
    ...(Array.isArray(turn.choiceOptions) ? turn.choiceOptions : [])
  ];
  for (const candidate of candidates) {
    if (!isRecord(candidate)) continue;
    const directRoot = requiredString(candidate.planningSelectionRootTurnId);
    const directPortfolio = requiredString(candidate.rootPortfolioId);
    if (directRoot && directPortfolio) {
      return { planningSelectionRootTurnId: directRoot, rootPortfolioId: directPortfolio };
    }
    const projection = comparisonProjectionFromUnknown(candidate.comparisonProjection);
    if (projection) {
      return {
        planningSelectionRootTurnId: projection.planningSelectionRootTurnId,
        rootPortfolioId: projection.rootPortfolioId
      };
    }
  }
  return null;
}

function isComparisonProjectionCarrier(turn: ConversationTurn): boolean {
  return turn.role === "assistant" && (turn.status === "active" || turn.status === "internal_capability");
}

function comparisonProjectionScopeKey(projection: ComparisonPlanProjection): string {
  return `${projection.planningSelectionRootTurnId}\u0000${projection.rootPortfolioId}`;
}

function isTraceQualifiedReadOnlyPartialProjection(
  turn: ConversationTurn,
  projection: ComparisonPlanProjection
): boolean {
  if (projection.isPartial !== true || projection.adoptionReady !== false) return false;
  const trace = turn.structuredChoiceTrace;
  const outcome = trace?.outcome;
  const reason = outcome && typeof outcome.reason === "string" ? outcome.reason : "";
  const qualifiedRaw = outcome?.qualifiedPartialProjectionIds;
  const qualified = new Set(
    Array.isArray(qualifiedRaw)
      ? qualifiedRaw.filter((value): value is string => typeof value === "string" && value.trim().length > 0)
      : []
  );
  const parsed = Array.isArray(turn.comparisonProjections)
    ? turn.comparisonProjections.map(comparisonProjectionFromUnknown)
    : [];
  const partials = parsed.filter(
    (candidate): candidate is ComparisonPlanProjection =>
      candidate !== null && candidate.isPartial === true && candidate.adoptionReady === false
  );
  const hasScopedIdentity = [
    trace?.sourceAssistantTurnId,
    trace?.resolvedChoiceId,
    trace?.planningSelectionRootTurnId,
    trace?.rootPortfolioId
  ].every((value) => typeof value === "string" && value.trim().length > 0);
  const choiceIdentity = [
    trace?.requestChoiceId,
    trace?.persistedChoiceId,
    trace?.resolvedChoiceId,
    trace?.executionChoiceId
  ];
  const hasBoundChoiceIdentity =
    choiceIdentity.every((value) => typeof value === "string" && value.trim().length > 0) &&
    new Set(choiceIdentity).size === 1;
  const hasZeroWriteEvidence = [outcome?.versionDelta, outcome?.patchDelta, outcome?.routeWriteDelta].every(
    (value) => typeof value === "number" && value === 0
  );
  return Boolean(
    trace?.executionStatus === "succeeded" &&
    trace.executionAction === "retry_model_planning" &&
    trace.executionRoute === "controller_choice_resume" &&
    ["new_grounded_partial_preview", "new_verified_partial_preview"].includes(reason) &&
    trace?.sourceAssistantTurnRole === "assistant" &&
    trace?.sourceAssistantTurnStatus === "active" &&
    hasScopedIdentity &&
    hasBoundChoiceIdentity &&
    hasZeroWriteEvidence &&
    partials.length > 0 &&
    partials.every(
      (candidate) =>
        candidate.sourceAssistantTurnId === turn.id &&
        candidate.planningSelectionRootTurnId === trace?.planningSelectionRootTurnId &&
        candidate.rootPortfolioId === trace?.rootPortfolioId &&
        qualified.has(candidate.proposalId)
    ) &&
    qualified.size === partials.length &&
    outcome?.partialProjectionDelta === partials.length &&
    projection.sourceAssistantTurnId === turn.id &&
    projection.planningSelectionRootTurnId === trace?.planningSelectionRootTurnId &&
    projection.rootPortfolioId === trace?.rootPortfolioId
  );
}

function comparisonProjectionUpdateMode(turn: ConversationTurn): "append" | "replace" | null {
  if (turn.comparisonProjectionUpdateMode === "append" || turn.comparisonProjectionUpdateMode === "replace") {
    return turn.comparisonProjectionUpdateMode;
  }
  if (turn.comparisonProjectionUpdateMode === null) {
    return null;
  }
  const trace = turn.structuredChoiceTrace;
  const outcome = trace?.outcome;
  const reason = outcome && typeof outcome.reason === "string" ? outcome.reason : "";
  const hasScopedIdentity = [
    trace?.sourceAssistantTurnId,
    trace?.resolvedChoiceId,
    trace?.planningSelectionRootTurnId,
    trace?.rootPortfolioId
  ].every((value) => typeof value === "string" && value.trim().length > 0);
  const choiceIdentity = [
    trace?.requestChoiceId,
    trace?.persistedChoiceId,
    trace?.resolvedChoiceId,
    trace?.executionChoiceId
  ];
  const hasBoundChoiceIdentity =
    choiceIdentity.every((value) => typeof value === "string" && value.trim().length > 0) &&
    new Set(choiceIdentity).size === 1;
  const hasZeroWriteEvidence = [outcome?.versionDelta, outcome?.patchDelta, outcome?.routeWriteDelta].every(
    (value) => typeof value === "number" && value === 0
  );
  const qualifiedPartialProjectionIdsRaw = outcome?.qualifiedPartialProjectionIds;
  const qualifiedPartialProjectionIds = new Set(
    Array.isArray(qualifiedPartialProjectionIdsRaw)
      ? qualifiedPartialProjectionIdsRaw.filter(
          (value): value is string => typeof value === "string" && value.trim().length > 0
        )
      : []
  );
  const parsedProjections = Array.isArray(turn.comparisonProjections)
    ? turn.comparisonProjections.map(comparisonProjectionFromUnknown)
    : [];
  const readOnlyPartialProjectionIds = new Set(
    parsedProjections
      .filter(
        (projection): projection is ComparisonPlanProjection =>
          projection !== null && projection.isPartial === true && projection.adoptionReady === false
      )
      .map((projection) => projection.proposalId)
  );
  const partialProjectionDelta = outcome?.partialProjectionDelta;
  const hasQualifiedReadOnlyPartialEvidence =
    readOnlyPartialProjectionIds.size === 0 ||
    ([
      "new_verified_proposal",
      "new_route_pending_proposal",
      "new_grounded_partial_preview",
      "new_verified_partial_preview"
    ].includes(reason) &&
      Array.isArray(qualifiedPartialProjectionIdsRaw) &&
      qualifiedPartialProjectionIdsRaw.length === qualifiedPartialProjectionIds.size &&
      qualifiedPartialProjectionIds.size === readOnlyPartialProjectionIds.size &&
      [...readOnlyPartialProjectionIds].every((proposalId) => qualifiedPartialProjectionIds.has(proposalId)) &&
      typeof partialProjectionDelta === "number" &&
      Number.isInteger(partialProjectionDelta) &&
      partialProjectionDelta === readOnlyPartialProjectionIds.size);
  const projectionScopeMatches =
    parsedProjections.length > 0 &&
    parsedProjections.every((projection) => {
      return (
        projection !== null &&
        projection.sourceAssistantTurnId === turn.id &&
        projection.planningSelectionRootTurnId === trace?.planningSelectionRootTurnId &&
        projection.rootPortfolioId === trace?.rootPortfolioId &&
        ((projection.isPartial === true &&
          projection.adoptionReady === false &&
          readOnlyPartialProjectionIds.has(projection.proposalId)) ||
          (turn.choiceOptions ?? []).some((option) => {
            const optionProjection = comparisonProjectionFromUnknown(option.comparisonProjection);
            return (
              String(option.id ?? "") === projection.choiceId &&
              optionProjection?.proposalId === projection.proposalId &&
              optionProjection.choiceId === projection.choiceId &&
              optionProjection.sourceAssistantTurnId === projection.sourceAssistantTurnId &&
              optionProjection.planningSelectionRootTurnId === projection.planningSelectionRootTurnId &&
              optionProjection.rootPortfolioId === projection.rootPortfolioId
            );
          }))
      );
    });
  if (
    trace?.executionStatus === "succeeded" &&
    trace.executionAction === "retry_model_planning" &&
    trace.executionRoute === "controller_choice_resume" &&
    [
      "new_verified_proposal",
      "new_route_pending_proposal",
      "new_grounded_partial_preview",
      "new_verified_partial_preview"
    ].includes(reason) &&
    hasScopedIdentity &&
    trace?.sourceAssistantTurnRole === "assistant" &&
    trace?.sourceAssistantTurnStatus === "active" &&
    hasBoundChoiceIdentity &&
    projectionScopeMatches &&
    hasQualifiedReadOnlyPartialEvidence &&
    hasZeroWriteEvidence
  ) {
    return "append";
  }
  return "replace";
}

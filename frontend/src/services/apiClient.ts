import { safeExternalHttpUrl } from "./safeExternalUrl";

export type ProviderStatus = {
  providerKind: string;
  providerName: string;
  isMock: boolean;
  status: "available" | "degraded" | "unavailable";
  sourceName?: string;
  sourceUrl?: string;
  queriedAt: string;
  confidence?: number;
  credibilityRank: string;
  userVisibleCaveat?: string;
  data: Record<string, unknown>;
};

export type ProviderStatusResponse = {
  mode: string;
  agent?: {
    providerName: string;
    configured: boolean;
    model: string;
    selectedModel?: string;
    availableModels?: Array<{ id: string; label: string }>;
    rawModel?: string;
    timeoutSeconds: number;
    status: "available" | "degraded" | "unavailable";
    userVisibleCaveat?: string;
  };
  tools?: {
    webSearch?: {
      providerName: string;
      configured: boolean;
      timeoutSeconds: number;
      status: "available" | "degraded" | "unavailable";
      userVisibleCaveat?: string;
    };
    amapWeather?: {
      providerName: string;
      configured: boolean;
      timeoutSeconds: number;
      status: "available" | "degraded" | "unavailable";
      userVisibleCaveat?: string;
    };
  };
  default: ProviderStatus[];
  mock: ProviderStatus[];
};

export type InspirationCreateResponse = {
  inspirationSetId: string;
  status: string;
  sourceMaterialIds: string[];
};

export type SourceMaterialUploadResponse = {
  sourceMaterialId: string;
  kind: string;
  thumbnailUrl?: string;
  thumbnailPlaceholder: boolean;
  originalRetention: string;
  cacheStatus: string;
};

export type ExtractionResponse = {
  inspirationSetId: string;
  cityCandidates: string[];
  poiCandidates: Array<{ name: string; confidence: number; sourceLinks: string[] }>;
  styleTags: string[];
  budgetClues: string[];
  routeClues: string[];
  confidence: number;
  needsUserConfirmation: boolean;
  sourceLinks: string[];
  providerName: string;
  fallbackUsed: boolean;
  providerFailureReason?: string;
  userVisibleCaveat?: string;
  itineraryDraft: ItineraryDraft;
};

export type CostItem = {
  label: string;
  amountCny: number;
  currency: string;
  isEstimate: boolean;
};

export type ItinerarySegment = {
  id: string;
  title: string;
  poiName: string;
  startTime: string;
  durationMinutes: number;
  transportMode: string;
  costItems: CostItem[];
  reservationNotes: string[];
};

export type ItineraryDay = {
  dayNumber: number;
  title: string;
  segments: ItinerarySegment[];
};

export type ItineraryDraft = {
  title: string;
  editable: boolean;
  days: ItineraryDay[];
};

export type PlannerPoi = {
  id: string;
  amapId?: string;
  name: string;
  city: string;
  category: string;
  latitude: number | null;
  longitude: number | null;
  photoUrl?: string;
  source: string;
  type?: string;
  district?: string;
  address?: string;
  sourceNote?: string;
  sourceUrl?: string;
  confidence: number;
  providerTypeCode?: string | null;
  tags?: string[];
  sourceClaims?: Array<Record<string, unknown>>;
  groundingStatus?:
    | "draft_only"
    | "routeable_anchor"
    | "verified_amap"
    | "agent_selected_candidate"
    | "user_confirmed"
    | "area_unresolved"
    | "waiting_for_poi_grounding"
    | "provider_rate_limited"
    | "area_poi"
    | "functional_poi"
    | "composite_poi"
    | string;
  poiSpecificity?: "exact_entity" | "area_poi" | "functional_poi" | "composite_poi" | string;
  intentType?: string | null;
  needsConcretePoi?: boolean;
  mapReady?: boolean;
  routeable?: boolean;
  matchedAmapName?: string | null;
  grounding?: {
    status: string;
    groundingStatus?:
      | "draft_only"
      | "routeable_anchor"
      | "verified_amap"
      | "agent_selected_candidate"
      | "user_confirmed"
      | "area_unresolved"
      | "waiting_for_poi_grounding"
      | "provider_rate_limited"
      | "area_poi"
      | "functional_poi"
      | "composite_poi"
      | string;
    poiSpecificity?: "exact_entity" | "area_poi" | "functional_poi" | "composite_poi" | string;
    intentType?: string | null;
    needsConcretePoi?: boolean;
    mapReady: boolean;
    routeable?: boolean;
    matchedAmapName?: string | null;
    hasProviderPoiId: boolean;
    hasCoordinates: boolean;
    needsVerification: boolean;
  };
};

export type PlannerSegment = {
  id: string;
  startTime: string;
  endTime: string;
  kind: string;
  poi: PlannerPoi;
  transportMode: string;
  estimatedCost: number;
  estimateMetadata?: {
    duration?: {
      minMinutes?: number;
      preferredMinutes?: number;
      maxMinutes?: number;
      source?: string;
      confidence?: number;
      userLocked?: boolean;
      [key: string]: unknown;
    };
    cost?: {
      min?: number;
      preferred?: number;
      max?: number;
      source?: string;
      confidence?: number;
      provisional?: boolean;
      status?: "confirmed" | "estimated" | "provisional" | "unknown" | string;
      [key: string]: unknown;
    };
    [key: string]: unknown;
  };
  semanticMetadata?: {
    schedulePreference?: Record<string, unknown>;
    scheduleConstraints?: Record<string, unknown>;
    scheduleDecision?: Record<string, unknown>;
    [key: string]: unknown;
  };
  notes: string;
};

export type SocialLinkIngestResponse = {
  sourceMaterialId: string;
  fetchStatus: "succeeded" | "needs_user_material" | string;
  failureReason?: string | null;
  canonicalUrl?: string | null;
  extractedText?: string | null;
};

export type PlannerDay = {
  id: string;
  dayNumber: number;
  title?: string;
  date?: string;
  weatherSummary: string;
  riskSummary: string;
  totalEstimatedCost: number;
  segments: PlannerSegment[];
  pendingSlots?: PendingTimelineSlot[];
};

export type PendingTimelineSlot = {
  id: string;
  planningSlotId: string;
  briefId: string;
  poolId?: string | null;
  dayNumber: number;
  timeWindow?: string;
  startTime?: string;
  endTime?: string;
  durationMinutes?: number;
  rawNeed: string;
  intentType: string;
  kind: string;
  state: "pending" | string;
  label: string;
  timingStatus?: "awaiting_route_confirmation" | "schedule_conflict_pending" | string;
  timingBasis?: string;
  constraintSummary?: string;
  placementAfterSegmentId?: string | null;
  placementBeforeSegmentId?: string | null;
};

export type RouteOption = {
  id: string;
  fromSegmentId?: string | null;
  toSegmentId?: string | null;
  fromPoiId: string;
  toPoiId: string;
  provider: string;
  mode: string;
  label: string;
  isSelected: boolean;
  sortOrder: number;
  transportMode: string;
  distanceMeters: number;
  durationSeconds: number;
  durationMinutes: number;
  costAmount: number;
  costCurrency: string;
  costEstimate: number;
  crowdingRisk: string;
  source: string;
  polyline: number[][];
  steps: Array<Record<string, unknown>>;
  providerPayload: Record<string, unknown>;
  error?: Record<string, unknown> | null;
  status?: string | null;
  routeStatus?: string | null;
  queriedAt: string;
};

export type WeatherSignal = {
  id: string;
  city: string;
  date: string;
  hourlyForecast: Array<Record<string, unknown>>;
  dailySummary: string;
  riskLevel: string;
  purposeImpactReason: string;
  source: string;
  dataStatus?: "available" | "degraded" | "unavailable" | string;
  confidence?: number;
  failureReason?: string | null;
  sourceUrl?: string | null;
  userVisibleCaveat?: string;
  providerName?: string;
  fallbackUsed?: boolean;
  queriedAt: string;
};

export type TrafficCrowdingSignal = {
  id: string;
  routeOptionId: string;
  realDataAvailable: boolean;
  crowdingLevel: string;
  estimatedReason: string;
  recommendedDepartureAdjustment: string;
  source: string;
  queriedAt: string;
};

export type PoiRiskAlert = {
  id: string;
  planId: string;
  segmentId: string;
  poiName: string;
  status: "available" | "degraded" | "unavailable" | string;
  summary: string;
  sourceName: string;
  sourceUrl?: string | null;
  sources: Array<{
    type?: string;
    title?: string;
    url?: string;
    snippet?: string;
    query?: string;
    riskStatusReason?: string;
    providerName?: string;
    attemptedProviders?: string[];
    successfulProviders?: string[];
    failedProviders?: string[];
    skippedProviders?: string[];
    providerDiagnostics?: Array<Record<string, unknown>>;
    [key: string]: unknown;
  }>;
  confidence: number;
  failureReason?: string | null;
  userVisibleCaveat: string;
  queriedAt: string;
};

export type TicketLookupResult = {
  id: string;
  segmentId: string;
  ticketType: string;
  status: string;
  priceEstimate: number;
  bookingUrl: string;
  sourceName: string;
  sourceUrl: string;
  credibilityRank: string;
  queriedAt: string;
  caveat: string;
  providerName: string;
  fallbackUsed: boolean;
  providerFailureReason?: string;
  confidence: number;
};

export type VisitFactItem = {
  status: "verified" | "advisory" | "unknown" | "conflicting" | "failed" | "not_applicable" | string;
  valueText: string;
  structuredValue?: Record<string, unknown> | null;
  effectiveForDate?: string | null;
  sourceRefs: Array<{ title?: string; url?: string; sourceName?: string; credibilityRank?: string }>;
  queriedAt: string;
  expiresAt: string;
  caveat?: string | null;
};

export type SegmentVisitFacts = {
  segmentId: string;
  amapPoiId: string;
  visitDate: string;
  refreshStatus: string;
  facts: {
    openingHours?: VisitFactItem;
    reservation?: VisitFactItem;
    ticketPrice?: VisitFactItem;
    ticketRelease?: VisitFactItem;
  };
  sourceRefs: Array<{ title?: string; url?: string; sourceName?: string; credibilityRank?: string }>;
  evidenceFingerprint: string;
  queriedAt: string;
  expiresAt: string;
};

export type PlanningToolCall = {
  id: string;
  toolName: string;
  status: "waiting" | "querying" | "completed" | "fallback" | "failed" | string;
  providerName: string;
  sourceName: string;
  sourceUrl?: string | null;
  queriedAt: string;
  confidence: number;
  fallbackUsed: boolean;
  failureReason?: string | null;
  userVisibleCaveat: string;
  summary: string;
  metadata?: Record<string, unknown>;
};

export type AgentPlanningEvent = {
  type: string;
  label: string;
  status: "waiting" | "querying" | "completed" | "fallback" | "failed" | string;
  detail: string;
  sessionId?: string | null;
  turnId?: string | null;
  providerName?: string | null;
  toolProvider?: string | null;
  fallbackUsed: boolean;
  failureReason?: string | null;
  durationMs?: number;
  sequence?: number;
  category?: "decision" | "tool_action" | "tool_result" | "timeline_effect" | "validation" | "internal" | string;
  goal?: string;
  actionLabel?: string;
  inputSummary?: string;
  resultSummary?: string;
  decisionSummary?: string;
  effectSummary?: string;
  userVisible?: boolean;
  metadata?: Record<string, unknown>;
  timestamp: string;
};

export type AgentReasoningStatus = {
  messageType: "reasoning_status";
  id: string;
  sequence: number;
  runId?: string;
  semanticKey?: string;
  phase:
    | "understanding"
    | "context"
    | "constraints"
    | "planning"
    | "tool"
    | "verification"
    | "finalizing"
    | "places"
    | "feasibility"
    | "result";
  status: "running" | "completed" | "fallback" | "failed" | "cancelled";
  summary: string;
  detail?: string | null;
  sourceEventType: string;
  sessionId?: string | null;
  turnId?: string | null;
  rootUserTurnId?: string | null;
  assistantTurnId?: string | null;
  startedAt?: string;
  completedAt?: string | null;
  elapsedMs?: number;
  firstSequence?: number;
  latestSequence?: number;
  workerCount?: number;
  completedWorkerCount?: number;
  timestamp: string;
};

export type AgentReasoningStatusSnapshot = {
  sessionId: string;
  sourceUserTurnId?: string | null;
  assistantTurnId?: string | null;
  statuses: AgentReasoningStatus[];
  active: boolean;
  nextSequence: number;
  terminalStatus?: "completed" | "failed" | "cancelled" | null;
};

export type AgentExecutionStreamItem =
  | { event: "user_turn"; data: ConversationTurn }
  | { event: "execution_event"; data: AgentPlanningEvent }
  | { event: "reasoning_status"; data: AgentReasoningStatus }
  | { event: "message_response"; data: AgentMessageResponse }
  | { event: "error"; data: { message?: string; statusCode?: number; [key: string]: unknown } };

export type SourceAssessment = {
  sourceName: string;
  sourceUrl?: string | null;
  credibilityRank: string;
  credibilityLabel: string;
  providerName: string;
  confidence: number;
  fallbackUsed: boolean;
  conflictDetected: boolean;
  conflictReason: string;
  recommendation: string;
};

export type FeasibilityIssue = {
  code: string;
  severity: "low" | "medium" | "high" | string;
  dimension: string;
  message: string;
  recommendation: string;
  affectedDayId?: string | null;
  affectedSegmentId?: string | null;
  evidence: string[];
};

export type LocalReplanSuggestion = {
  id: string;
  issueCode: string;
  actionType: string;
  summary: string;
  rationale: string;
  requiresConfirmation: boolean;
  operations: Array<Record<string, unknown>>;
};

export type FeasibilityReport = {
  score: number;
  riskLevel: "low" | "medium" | "high" | string;
  issues: FeasibilityIssue[];
  suggestions: string[];
  localReplanSuggestions: LocalReplanSuggestion[];
  preferenceAlignment: string;
  checkedAt: string;
};

export type PlanningRun = {
  id: string;
  runType: string;
  userInput: string;
  preferenceSummary: string;
  itineraryPlanId?: string | null;
  itineraryVersionId?: string | null;
  understoodRequirements: {
    summary?: string;
    missingFields?: string[];
    clarificationQuestions?: string[];
    isCompleteEnoughToPlan?: boolean;
    [key: string]: unknown;
  };
  constraintSummary: Array<{ label?: string; value?: unknown }>;
  toolCalls: PlanningToolCall[];
  sourceAssessments: SourceAssessment[];
  feasibilityReport?: FeasibilityReport | null;
  finalSummary: string;
  createdAt: string;
};

export type PlanningTraceExport = {
  schemaVersion: "trip-planning-trace-v1";
  scope: {
    sessionId: string;
    assistantTurnId: string;
    planningRunId: string;
  };
  [key: string]: unknown;
};
export type ItineraryPlan = {
  id: string;
  title: string;
  city: string;
  templateType: string;
  budgetTarget?: number | null;
  budgetTier?: "low" | "medium" | "high" | "unknown" | string;
  budgetEstimate: number;
  budgetBreakdown?: BudgetBreakdown | null;
  budgetDeltaExplanation: string;
  decisionRationale: string;
  status: string;
  days: PlannerDay[];
  routeOptions: RouteOption[];
  weatherSignals: WeatherSignal[];
  trafficCrowdingSignals: TrafficCrowdingSignal[];
  poiRiskAlerts?: PoiRiskAlert[];
  ticketLookupResults: TicketLookupResult[];
  visitFactsBySegment?: Record<string, SegmentVisitFacts>;
  routeWarnings?: string[];
  feasibilityReport?: FeasibilityReport | null;
  localReplanSuggestions?: LocalReplanSuggestion[];
};

export type ItineraryPlanEnvelope = {
  plan: ItineraryPlan;
  planningRun?: PlanningRun | null;
};

export type AgentStructuredChoiceTrace = {
  sourceAssistantTurnId?: string | null;
  resolvedChoiceId?: string | null;
  executionStatus?: "succeeded" | AgentChoiceOption["lifecycle"] | null;
  executionRoute?: string | null;
  controllerCalled?: boolean | null;
  controlOwner?: string | null;
  checkpointFingerprint?: string | null;
  runtimeBuildId?: string | null;
  runtimeStartedAt?: string | null;
  outcome?: Record<string, unknown> | null;
  [key: string]: unknown;
};

export type ClarificationCheckpoint = {
  checkpointId: string;
  schemaVersion?: string;
  planningRootId?: string;
  requestFingerprint?: string;
  contractVersion?: number;
  status?: "active" | "resolved" | "awaiting_answer" | "awaiting_agent_resolution" | "answered" | string;
  ambiguities?: Array<{
    dimensionId: string;
    impact?: string;
    candidateScope?: Record<string, unknown>;
    resolved?: boolean;
    answerSource?: string | null;
    allowedSemanticValues?: unknown[];
  }>;
  resolvedAnswers?: Array<{
    dimensionId: string;
    semanticValue: unknown;
    source?: string;
    sourceUserTurnId?: string;
    optionId?: string | null;
    label?: string;
  }>;
  /** @deprecated Compatibility with early deterministic UI fixtures. */
  answers?: Array<{
    dimensionId: string;
    semanticValue: unknown;
    label?: string;
  }>;
  /** @deprecated Compatibility with early deterministic UI fixtures. */
  resolvedDimensions?: string[];
  /** @deprecated Compatibility with early deterministic UI fixtures. */
  unresolvedDimensions?: string[];
  experienceSpecs?: Array<Record<string, unknown>>;
  candidateGapSummary?: Record<string, unknown>;
  nextQuestionDimensionId?: string | null;
  completionCriteria?: unknown[];
  sourceUserTurnId?: string;
  sourceAssistantTurnId?: string;
  fingerprint?: string;
  submissionMode?: "batch_atomic";
  submitChoiceId?: string;
  questions?: ClarificationBatchQuestion[];
  spatialMapSelections?: Record<string, Record<string, unknown>>;
  question?: {
    dimensionId?: string;
    question?: string;
    whyItMatters?: string;
    options?: Array<{
      id?: string;
      label?: string;
      semanticValue?: unknown;
    }>;
    allowFreeText?: boolean;
  };
  [key: string]: unknown;
};

export type SpatialMapSelectionBindResponse = {
  optionId: string;
  label: string;
  mapSelectionFingerprint: string;
  checkpoint: ClarificationCheckpoint;
};

export type ClarificationBatchQuestion = {
  dimensionId: string;
  question: string;
  whyItMatters: string;
  required: true;
  allowFreeText: boolean;
  options: Array<{
    id: string;
    label: string;
    semanticValue: Record<string, unknown>;
  }>;
};

export type ClarificationBatchSelection = {
  dimensionId: string;
  optionId?: string;
  manualValue?: string;
};

export type ClarificationSubmission = {
  checkpointId: string;
  sourceAssistantTurnId: string;
  requestUserTurnId: string;
  executionId: string;
  status: "succeeded";
  answers: Array<{
    dimensionId: string;
    optionId?: string;
    label: string;
    source: string;
  }>;
};

export type TravelGuideAdvice = {
  status?: "completed" | "no_results" | "failed";
  failureReason?: string | null;
  recommendations: Array<{
    refId?: string;
    sourceFingerprint?: string;
    title?: string;
    text: string;
    sourceUrl?: string;
    sourceName?: string;
    queriedAt?: string;
    credibilityRank?: string;
    summaryKind?: "search_result_snippet" | string;
    poiVerificationStatus: "unverified_advice";
  }>;
  cautions: Array<{ text: string; sourceUrl?: string }>;
  sourceRefs: Array<{
    refId?: string;
    sourceFingerprint?: string;
    title: string;
    url: string;
    sourceName?: string;
    queriedAt?: string;
    credibilityRank?: string;
  }>;
  queryFingerprint: string;
  evidenceFingerprint?: string;
  queriedAt: string;
  caveat: string;
  queryCount: number;
  relevanceFilter?: {
    acceptedResultCount: number;
    rejectedResultCount: number;
    reasonCounts: Record<string, number>;
  };
  attemptedProviders?: string[];
  successfulProviders?: string[];
  failedProviders?: string[];
  skippedProviders?: string[];
  providerDiagnostics?: Array<Record<string, unknown>>;
  conclusion?: {
    status: "ready" | "partial" | "insufficient_evidence" | "conflicting" | string;
    overview: string;
    takeaways: Array<{ intentType: string; themeLabel: string; text: string; sourceRefIds: string[] }>;
    conflicts: Array<{ topic: string; summary: string; sourceRefIds: string[] }>;
    missingThemes: Array<{ intentType: string; themeLabel: string }>;
    evidenceBasis: "search_result_snippets" | string;
    generationMethod: "deepseek_structured_v1" | "deterministic_fallback_v1" | string;
    fallbackReasonCode?: string;
  };
  /** Search-derived hints are advisory only and still require AMap grounding. */
  placeHints?: TravelGuidePlaceHint[];
};

export type TravelGuidePlaceHint = {
  schemaVersion: "guide-place-hint-v1" | string;
  mentionText: string;
  intentType: string;
  sourceRefIds: string[];
  sourceFingerprints: string[];
  guideEvidenceFingerprint: string;
  verificationStatus: "unresolved_amap_grounding" | string;
};

export type GuideEvidenceUsagePlace = {
  mentionText: string;
  intentType: string;
  sourceRefIds: string[];
  amapPoiId: string;
  physicalIdentityKey: string;
  dayNumber: number;
  planningSlotId: string;
  routeVerified: boolean;
};

export type GuideEvidenceUsage = {
  schemaVersion: "guide-evidence-usage-v1" | string;
  status: "satisfied" | string;
  evidenceFingerprint: string;
  requiredMinimum: number;
  usedPlaces: GuideEvidenceUsagePlace[];
  rejectionCounts: Record<string, number>;
};

export type ProposalSegmentVisitFacts = SegmentVisitFacts & {
  openingHours?: VisitFactItem;
  scheduleCompatibility?:
    | "verified_compatible"
    | "verified_conflict"
    | "verified_unknown_schedule"
    | "unknown"
    | string;
  scheduledStartTime?: string;
  scheduledEndTime?: string;
};

export type ProposalVisitFactsRefreshResponse = {
  proposalId: string;
  materialFingerprint: string;
  refreshStatus: "completed" | "partial" | "failed" | string;
  visitFactsBySegment: Record<string, ProposalSegmentVisitFacts>;
  verifiedScheduleConflicts: Array<Record<string, unknown>>;
  queriedAt: string;
  omittedSegmentCount?: number;
};

export type ComparisonFrontierStatus =
  | "has_more"
  | "provider_pending"
  | "qualification_exhausted"
  | "poi_exhausted"
  | "route_feasible_exhausted";

export type ComparisonSummary = {
  adoptionReadyCount: number;
  repairablePartialCount: number;
  remainingQualifiedEntityCount: number;
  remainingPoiPageCount?: number | null;
  frontierStatus: ComparisonFrontierStatus;
  lastOutcomeReason?: string | null;
  exploredQualifiedEntityCount?: number | null;
  attemptedPoiPageCount?: number | null;
  blockingLayer?: string | null;
  constraintModificationChoiceId?: string | null;
  constraintModificationLabel?: string | null;
};

export type SharedTravelSource = {
  schemaVersion: "shared-travel-source-v1";
  status: "completed" | "needs_user_material";
  sourceMaterialId: string;
  canonicalUrl: string | null;
  title: string | null;
  bodyText: string | null;
  contentFingerprint: string | null;
  imageCount: number;
  imageStatus: "not_read";
  failureReason?: string | null;
};

export type ConversationTurn = {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  turnIndex: number;
  status: "active" | "superseded" | "failed" | "internal_capability";
  parentTurnId?: string;
  itineraryVersionId?: string;
  planningRunId?: string | null;
  failureReason?: string | null;
  comparisonProjections?: Record<string, unknown>[];
  comparisonProjectionUpdateMode?: "append" | "replace" | null;
  planningSelectionRootTurnId?: string | null;
  rootPortfolioId?: string | null;
  comparisonSummary?: ComparisonSummary | null;
  choiceOptions?: AgentChoiceOption[];
  localPoiOptions?: Record<string, unknown> | null;
  structuredChoiceTrace?: AgentStructuredChoiceTrace | null;
  clarificationCheckpoint?: ClarificationCheckpoint | null;
  clarificationSubmission?: ClarificationSubmission | null;
  guideAdvice?: TravelGuideAdvice | null;
  sharedSource?: SharedTravelSource | null;
  experienceSpecs?: Array<Record<string, unknown>>;
  candidateGapSummary?: Record<string, unknown> | null;
  spatialBoundaryPreview?: SpatialBoundaryPreview | null;
  planningDirectionCount?: number | null;
  visibleComparisonProposalCount?: number | null;
  partialComparisonProposalCount?: number | null;
  verifiedComparisonProposalCount?: number | null;
  adoptionReadyProposalCount?: number | null;
  timelineMutationOutcome?: Record<string, unknown> | null;
  timelineMutationTransaction?: Record<string, unknown> | null;
  planningSteps?: AgentPlanningEvent[];
  toolEvents?: AgentPlanningEvent[];
  reasoningStatuses?: AgentReasoningStatus[];
  createdAt: string;
  updatedAt: string;
};

export type BudgetBreakdown = {
  tier: string;
  numericTarget: number | null;
  knownActivityCost: number;
  knownMealCost: number;
  knownTransportCost: number;
  knownTotal: number;
  provisionalMin: number;
  provisionalPreferred: number;
  provisionalMax: number;
  unknownItems: string[];
  isComplete: boolean;
};

export type AgentChoiceOption = {
  id?: string;
  index?: number;
  label?: string;
  value?: string | Record<string, unknown>;
  semanticValue?: string | Record<string, unknown>;
  dimensionId?: string;
  checkpointId?: string;
  kind?: string;
  action?:
    | "retry_model_planning"
    | "continue_plan_expansion"
    | "search_travel_guide_advice"
    | "confirm_rule_safe_draft"
    | "select_plan_proposal"
    | "adopt_active_partial"
    | "attempt_portfolio_theme_completion"
    | "confirm_portfolio_theme_upgrade"
    | "confirm_portfolio_theme_replacement"
    | "reject_portfolio_theme_replacement"
    | "manual_continuation"
    | "resume_density_candidate"
    | "refresh_density_candidates"
    | "expand_density_nearby"
    | "open_density_map"
    | "open_map_selection"
    | "execute_timeline_mutation_choice"
    | "execute_timeline_mutation_manual"
    | "continue_clarification"
    | "submit_clarification_batch"
    | "select_spatial_boundary_candidate"
    | "confirm_spatial_boundary"
    | "change_spatial_boundary"
    | "retry_spatial_grounding";
  scopeKind?: "clarification" | "comparison" | "timeline";
  checkpointFingerprint?: string;
  lifecycle?:
    | "offered"
    | "executing"
    | "consumed"
    | "failed_retryable"
    | "failed_terminal"
    | "expired"
    | "stale"
    | "cancelled";
  allowsManualInput?: boolean;
  sourceDecisionId?: string | null;
  sourceObservationFingerprint?: string | null;
  sourceUserTurnId?: string | null;
  expectedBaseVersionId?: string | null;
  requestIntentContractFingerprint?: string | null;
  attempt?: number;
  custom?: boolean;
  candidateRecordId?: string;
  amapId?: string;
  segmentId?: string;
  amapPoi?: MapPoi;
  selectionGroupId?: string;
  planningSlotId?: string;
  poolId?: string;
  dayNumber?: number;
  densityGroups?: Array<Record<string, unknown>>;
  [key: string]: unknown;
};

export type PendingPoiCandidate = {
  id: string;
  query: string;
  city: string;
  category: string;
  status: "pending" | "selected" | "rejected" | "expired";
  candidates: MapPoi[];
  selectedAmapId?: string;
  sourceSegmentId?: string | null;
  createdAt: string;
};

export type AgentSessionSummary = {
  sessionId: string;
  status: "active" | "archived";
  city: string;
  title: string;
  activePlanId: string;
  activeVersionId?: string | null;
  turnCount: number;
  updatedAt: string;
  createdAt: string;
};

export type AgentSessionListResponse = {
  sessions: AgentSessionSummary[];
};

export type AgentSession = {
  sessionId: string;
  status: "active" | "archived";
  city: string;
  title: string;
  activePlanId: string;
  activeVersionId?: string | null;
  turns: ConversationTurn[];
  itinerary: ItineraryPlan | null;
  pendingPoiCandidates: PendingPoiCandidate[];
  preferenceMemory?: PreferenceMemory | null;
  planningRun?: PlanningRun | null;
};

export type SpatialBoundaryPreview = {
  schemaVersion: "spatial-boundary-preview-v1";
  status: "confirmation_pending";
  canonicalName: string;
  boundaryEvidenceFingerprint: string;
  sourceUrl: string;
  sourceEntityId: string;
  sourceVersion?: string | null;
  retrievedAt: string;
  contentHash: string;
  license: "ODbL-1.0" | string;
  attribution: string;
  originalVertexCount: number;
  simplifiedVertexCount: number;
  simplificationMaxDeviationMeters: number;
  polygonGcj02: Array<[number, number]>;
};

export type SaveActiveDirectionRequest = {
  baseVersionId: string;
  planningSelectionRootTurnId: string;
  rootPortfolioId: string;
};

export type SaveActiveDirectionResponse = {
  proposalId: string;
  activeVersionId: string;
  comparisonProjection: Record<string, unknown>;
  saved: boolean;
  unchanged: boolean;
};

export type SavedItineraryVersion = {
  id: string;
  sessionId: string;
  planId: string;
  versionId: string;
  title: string;
  createdAt: string;
};

export type SavedItineraryVersionsResponse = {
  savedVersions: SavedItineraryVersion[];
};

export type ItineraryExportJson = {
  exportedAt: string;
  activeVersionId?: string | null;
  planId: string;
  itineraryPlan: ItineraryPlan;
  routeOptions: RouteOption[];
  riskSignals: {
    weatherSignals: WeatherSignal[];
    trafficCrowdingSignals: TrafficCrowdingSignal[];
    poiRiskAlerts: PoiRiskAlert[];
  };
};

export type AgentMessageResponse = {
  requestReplay?: { replayed: boolean; isHistorical: boolean } | null;
  userTurn: ConversationTurn;
  assistantTurn: ConversationTurn;
  itinerary: ItineraryPlan | null;
  version?: {
    id: string;
    versionNumber: number;
    sourceType: string;
  } | null;
  pendingPoiCandidates: PendingPoiCandidate[];
  preferenceMemory?: PreferenceMemory | null;
  warnings: string[];
  planningRun?: PlanningRun | null;
  planningSteps?: AgentPlanningEvent[];
  toolEvents?: AgentPlanningEvent[];
  reasoningStatuses?: AgentReasoningStatus[];
  executionMode?: "deterministic_fast_path" | "bounded_agent" | "deterministic_fallback" | string;
  terminalStatus?: "success" | "needs_confirmation" | "partial_success" | "no_safe_action" | "failed" | string;
  agentDecisionCount?: number;
  outcomeStatuses?: {
    itineraryStatus?: string;
    mapStatus?: string;
    routeStatus?: string;
    reservationStatus?: string;
    sourceQualityStatus?: string;
  };
};

export type AgentMessageEditResponse = {
  editedTurn: ConversationTurn;
  supersededTurnIds: string[];
  restoredVersionId: string | null;
  assistantTurn?: ConversationTurn | null;
  itinerary: ItineraryPlan | null;
  version?: {
    id: string;
    versionNumber: number;
    sourceType: string;
  } | null;
  pendingPoiCandidates: PendingPoiCandidate[];
  warnings: string[];
  planningRun?: PlanningRun | null;
  planningSteps?: AgentPlanningEvent[];
  toolEvents?: AgentPlanningEvent[];
};

export type ItineraryPatchOperation =
  | { op: "replace_itinerary"; fullItinerary: Record<string, unknown> }
  | { op: "replace_trip_title"; value: string }
  | { op: "replace_day_title"; dayId: string; value: string }
  | { op: "replace_segment_start_time"; segmentId: string; startTime?: string; value?: string }
  | { op: "replace_transport_mode"; segmentId: string; value: string }
  | { op: "add_day"; title?: string; value?: string }
  | {
      op: "add_segment";
      dayId: string;
      startTime?: string;
      value?: string;
      title?: string;
      kind?: string;
      notes?: string;
      durationMinutes?: number;
      estimatedCost?: number;
      transportMode?: string;
      allowUnresolved?: boolean;
      amapPoi?: MapPoi;
    }
  | { op: "replace_segment_poi"; segmentId: string; amapPoi: MapPoi; notes?: string }
  | {
      op: "replace_segment_poi_from_candidate";
      segmentId: string;
      candidateId: string;
      amapPoi: MapPoi;
      notes?: string;
    }
  | { op: "expand_area_poi_candidates"; segmentId: string; value?: string; radius?: number }
  | { op: "expand_meal_poi_candidates"; segmentId: string; value?: string; radius?: number }
  | { op: "confirm_poi_anchor"; segmentId: string }
  | { op: "refresh_ticket_for_segment"; segmentId: string }
  | { op: "refresh_routes_for_day"; dayId: string }
  | { op: "remove_segment"; segmentId: string }
  | { op: "move_segment"; segmentId: string; targetDayId?: string; dayId?: string; startTime?: string; value?: string }
  | { op: "reorder_segments"; dayId: string; orderedSegmentIds: string[] }
  | { op: "update_segment_notes"; segmentId: string; notes?: string; value?: string };

export type ItineraryPatchResponse = {
  itinerary: ItineraryPlan;
  patch: {
    id: string;
    validationStatus: "accepted" | "rejected";
    metadata?: Record<string, unknown>;
  };
  version: {
    id: string;
    versionNumber: number;
    sourceType: string;
  };
  validationErrors: string[];
  planningRun?: PlanningRun | null;
  pendingPoiCandidates: PendingPoiCandidate[];
};

export type PlanComparisonResponse = {
  comparisonId: string;
  providerName: string;
  fallbackUsed: boolean;
  userVisibleCaveat: string;
  plans: ItineraryPlan[];
  planningRun?: PlanningRun | null;
};

export type PreferenceSummaryCard = {
  id: string;
  profileId: string;
  partySize: number;
  travelerTypes: string[];
  budgetRange: string;
  pacePreference: string;
  summaryText: string;
  items: Array<{ label: string; sourceText: string }>;
  status: string;
  providerName?: string;
  fallbackUsed?: boolean;
  providerFailureReason?: string;
  userVisibleCaveat?: string;
};

export type PreferenceEnvelope = {
  summaryCard: PreferenceSummaryCard;
};

export type PreferenceMemory = {
  userId: string;
  sessionId?: string | null;
  memoryText: string;
  structuredMemory?: {
    version?: string;
    facts?: Array<{
      id: string;
      scope: "trip" | "session" | "global" | string;
      category: string;
      key: string;
      value: string;
      status: "confirmed" | "inferred" | "needs_confirmation" | string;
      source?: string;
      confidence?: number;
      evidence?: string;
      updatedAt?: string;
    }>;
    autoUpdateClassifications?: Array<Record<string, unknown>>;
  };
  compiledRules?: Record<string, unknown>;
  pendingConfirmations?: Array<Record<string, unknown>>;
  autoUpdateEnabled: boolean;
  createdAt: string;
  updatedAt: string;
  memoryDiagnostics?: {
    dbPath?: string;
    userId?: string;
    sessionId?: string | null;
    source?: "session" | "global" | string;
    globalMemoryUpdatedAt?: string | null;
    sessionMemoryUpdatedAt?: string | null;
    summaryCardCount?: number;
    autoUpdateEnabled?: boolean;
    frontendLoadedFrom?: "api" | "localStorage" | "default" | string;
  };
};

export type WeatherReminderResponse = {
  reminderId: string;
  simulatedStatus: string;
  subject: string;
  bodyPreview: string;
  providerName: string;
};

export type SourceCleanupResponse = {
  clearedCount: number;
  retainedLongTermCount: number;
};

export type MapConfigResponse = {
  provider: "amap";
  enabled: boolean;
  jsApiKey: string;
  securityJsCode?: string;
};

export type MapPoiPhoto = {
  title: string;
  url: string;
};

export type MapPoi = {
  id: string;
  amapId?: string;
  name: string;
  type: string;
  city: string;
  district: string;
  address: string;
  longitude: number;
  latitude: number;
  category: string;
  source: string;
  sourceNote: string;
  distanceMeters?: number | null;
  sourceUrl?: string | null;
  confidence: number;
  providerTypeCode?: string | null;
  tags?: string[];
  sourceClaims?: Array<Record<string, unknown>>;
  photos: MapPoiPhoto[];
  routeImpact?: {
    previousAnchor?: string | null;
    nextAnchor?: string | null;
    searchMode?: string;
    totalDistanceKm?: number;
    addedDistanceKm?: number;
    estimatedDurationMinutes?: number;
    addedDurationMinutes?: number;
    detourLevel?: "low" | "medium" | "high" | "unacceptable" | "unknown" | string;
    reason?: string;
  };
  reason?: string;
  nightViewReason?: string;
};

export type MapPoiSearchResponse = {
  city: string;
  keyword: string;
  category: string;
  providerName: string;
  queriedAt: string;
  pois: MapPoi[];
  cacheHit?: boolean;
};

export type MapPoiResolveQuery = {
  name: string;
  category?: string;
  near?: {
    longitude: number;
    latitude: number;
    radius?: number;
  } | null;
};

export type MapPoiResolveResponse = {
  resolved: Array<{
    query: string;
    status: "accepted";
    poi: MapPoi;
  }>;
  pending: Array<{
    candidateRecordId: string;
    query: string;
    reason: string;
    candidates: MapPoi[];
  }>;
};

export type InspirationPayload = {
  cityHint: string;
  textItems: string[];
  socialLinks: string[];
  sourceMaterialIds?: string[];
  saveOriginalImages?: boolean;
  planningContext?: Record<string, unknown>;
};

export type InspirationInputPayload = {
  cityHint: string;
  textItems: string[];
  socialLinks: string[];
  files: File[];
  sourceKind: string;
  saveOriginalImages: boolean;
  retryCandidateHints?: boolean;
  selectedAgentChoice?: {
    sourceAssistantTurnId: string;
    choiceId: string;
    manualValue?: string;
    batchSelections?: ClarificationBatchSelection[];
  };
  manualCandidateHints?: Array<{
    poolId?: string;
    intentType?: string;
    candidateHints: string[];
    hintPolicy?: "user_explicit_hint" | "llm_common_knowledge_hint" | "no_hint";
  }>;
};

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000/api";

export type ApiErrorBody = {
  code?: string;
  message?: string;
  details?: unknown;
  validationErrors?: unknown[];
  detail?: unknown;
};

export class ApiError extends Error {
  status: number;
  code: string;
  details?: unknown;
  validationErrors: unknown[];

  constructor(
    message: string,
    options: { status: number; code?: string; details?: unknown; validationErrors?: unknown[] }
  ) {
    super(message);
    this.name = "ApiError";
    this.status = options.status;
    this.code = options.code ?? `HTTP_${options.status}`;
    this.details = options.details;
    this.validationErrors = options.validationErrors ?? [];
  }
}

async function sendAgentMessageStreamRequest(
  sessionId: string,
  payload: {
    requestId?: string;
    content: string;
    agentModel?: string;
    context?: Record<string, unknown>;
  },
  options: {
    onExecutionEvent?: (event: AgentPlanningEvent) => void;
    onReasoningStatus?: (status: AgentReasoningStatus) => void;
    onUserTurn?: (turn: ConversationTurn) => void;
    signal?: AbortSignal;
  } = {}
): Promise<AgentMessageResponse> {
  const response = await fetch(`${API_BASE_URL}/agent/sessions/${sessionId}/messages/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
    signal: options.signal
  });
  if (!response.ok) {
    throw new ApiError(`API request failed: ${response.status} ${response.statusText}`, {
      status: response.status,
      code: `HTTP_${response.status}`
    });
  }
  if (!response.body) {
    throw new ApiError("Agent execution stream is not available in this environment", {
      status: 0,
      code: "STREAM_UNAVAILABLE"
    });
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let finalResponse: AgentMessageResponse | null = null;

  async function consumeLine(line: string) {
    if (!line.trim()) {
      return;
    }
    const item = JSON.parse(line) as AgentExecutionStreamItem;
    if (item.event === "user_turn") {
      options.onUserTurn?.(item.data);
      return;
    }
    if (item.event === "execution_event") {
      options.onExecutionEvent?.(item.data);
      return;
    }
    if (item.event === "reasoning_status") {
      options.onReasoningStatus?.(item.data);
      return;
    }
    if (item.event === "message_response") {
      finalResponse = item.data;
      return;
    }
    if (item.event === "error") {
      const streamError = item.data as { message?: string; statusCode?: number; code?: string; details?: unknown };
      throw new ApiError(streamError.message || "Agent execution stream failed", {
        status: streamError.statusCode ?? 500,
        code: streamError.code ?? "AGENT_STREAM_ERROR",
        details: streamError.details ?? streamError
      });
    }
  }

  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value ?? new Uint8Array(), { stream: !done });
    const lines = buffer.split(/\r?\n/);
    buffer = lines.pop() ?? "";
    for (const line of lines) {
      await consumeLine(line);
    }
    if (done) {
      break;
    }
  }
  await consumeLine(buffer);
  if (!finalResponse) {
    throw new ApiError("Agent execution stream ended without a final response", {
      status: 0,
      code: "STREAM_MISSING_FINAL_RESPONSE"
    });
  }
  return normalizeAgentMessageResponse(finalResponse);
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const isFormData = init?.body instanceof FormData;
  const response = await fetch(`${API_BASE_URL}${path}`, {
    headers: isFormData
      ? init?.headers
      : {
          "Content-Type": "application/json",
          ...init?.headers
        },
    ...init
  });

  if (!response.ok) {
    let message = `API request failed: ${response.status} ${response.statusText}`;
    let code = `HTTP_${response.status}`;
    let details: unknown;
    let validationErrors: unknown[] = [];
    try {
      const body = (await response.json()) as ApiErrorBody;
      const detailRecord = isRecord(body.detail) ? body.detail : null;
      code = body.code ?? (typeof detailRecord?.code === "string" ? detailRecord.code : code);
      details = body.details ?? body.detail;
      validationErrors = Array.isArray(body.validationErrors) ? body.validationErrors : [];
      if (typeof body.message === "string") {
        message = body.message;
      } else if (typeof body.detail === "string") {
        message = body.detail;
      } else if (typeof detailRecord?.message === "string") {
        message = detailRecord.message;
      } else if (body.detail) {
        message = JSON.stringify(body.detail);
      }
    } catch {
      // Keep the HTTP status message when the response is not JSON.
    }
    throw new ApiError(message, { status: response.status, code, details, validationErrors });
  }

  return response.json() as Promise<T>;
}

async function requestText(path: string, init?: RequestInit): Promise<string> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    headers: {
      ...init?.headers
    },
    ...init
  });

  if (!response.ok) {
    let message = `API request failed: ${response.status} ${response.statusText}`;
    let code = `HTTP_${response.status}`;
    let details: unknown;
    try {
      const body = (await response.json()) as ApiErrorBody;
      const detailRecord = isRecord(body.detail) ? body.detail : null;
      code = body.code ?? (typeof detailRecord?.code === "string" ? detailRecord.code : code);
      details = body.details ?? body.detail;
      if (typeof body.message === "string") {
        message = body.message;
      } else if (typeof body.detail === "string") {
        message = body.detail;
      } else if (typeof detailRecord?.message === "string") {
        message = detailRecord.message;
      }
    } catch {
      // Keep the HTTP status message when the response is not JSON.
    }
    throw new ApiError(message, { status: response.status, code, details });
  }

  return response.text();
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

function arrayOrEmpty<T>(value: unknown): T[] {
  return Array.isArray(value) ? (value as T[]) : [];
}

function validatePlanningTraceExport(value: unknown, expectedScope: PlanningTraceExport["scope"]): PlanningTraceExport {
  if (!isRecord(value) || value.schemaVersion !== "trip-planning-trace-v1") {
    throw new ApiError("规划 Trace 响应格式不受支持。", {
      status: 0,
      code: "INVALID_PLANNING_TRACE_SCHEMA"
    });
  }
  const scope = value.scope;
  if (
    !isRecord(scope) ||
    scope.sessionId !== expectedScope.sessionId ||
    scope.assistantTurnId !== expectedScope.assistantTurnId ||
    scope.planningRunId !== expectedScope.planningRunId
  ) {
    throw new ApiError("规划 Trace 响应范围与请求不一致。", {
      status: 0,
      code: "INVALID_PLANNING_TRACE_SCOPE"
    });
  }
  return value as PlanningTraceExport;
}

function requireRecordField<T>(value: unknown, fieldName: string, code: string): T {
  if (!isRecord(value)) {
    throw new ApiError(`API response missing ${fieldName}`, { status: 0, code });
  }
  return value as T;
}

function normalizeItineraryPlan(value: unknown): ItineraryPlan {
  const plan = requireRecordField<ItineraryPlan>(value, "itinerary", "INVALID_ITINERARY_RESPONSE");
  const raw = plan as unknown as Record<string, unknown>;
  return {
    ...plan,
    days: arrayOrEmpty<PlannerDay>(raw.days).map((day) => ({
      ...day,
      segments: arrayOrEmpty<PlannerSegment>(day.segments),
      pendingSlots: arrayOrEmpty<PendingTimelineSlot>(day.pendingSlots)
    })),
    routeOptions: arrayOrEmpty<RouteOption>(raw.routeOptions),
    weatherSignals: arrayOrEmpty<WeatherSignal>(raw.weatherSignals),
    trafficCrowdingSignals: arrayOrEmpty<TrafficCrowdingSignal>(raw.trafficCrowdingSignals),
    ticketLookupResults: arrayOrEmpty<TicketLookupResult>(raw.ticketLookupResults),
    visitFactsBySegment: isRecord(raw.visitFactsBySegment)
      ? (raw.visitFactsBySegment as Record<string, SegmentVisitFacts>)
      : {},
    poiRiskAlerts: arrayOrEmpty<PoiRiskAlert>(raw.poiRiskAlerts),
    routeWarnings: arrayOrEmpty<string>(raw.routeWarnings),
    localReplanSuggestions: arrayOrEmpty<LocalReplanSuggestion>(raw.localReplanSuggestions)
  };
}

function normalizeNullableItineraryPlan(value: unknown): ItineraryPlan | null {
  return value ? normalizeItineraryPlan(value) : null;
}

function normalizeTravelGuideAdvice(value: unknown): TravelGuideAdvice | null {
  if (!isRecord(value)) {
    return null;
  }
  const recommendations = arrayOrEmpty<Record<string, unknown>>(value.recommendations)
    .filter(isRecord)
    .map((item) => ({
      refId: typeof item.refId === "string" ? item.refId : undefined,
      sourceFingerprint: typeof item.sourceFingerprint === "string" ? item.sourceFingerprint : undefined,
      title: typeof item.title === "string" ? item.title : undefined,
      text: typeof item.text === "string" ? item.text : "",
      sourceUrl: safeExternalHttpUrl(item.sourceUrl),
      sourceName: typeof item.sourceName === "string" ? item.sourceName : undefined,
      queriedAt: typeof item.queriedAt === "string" ? item.queriedAt : undefined,
      credibilityRank: typeof item.credibilityRank === "string" ? item.credibilityRank : undefined,
      summaryKind: typeof item.summaryKind === "string" ? item.summaryKind : undefined,
      poiVerificationStatus: "unverified_advice" as const
    }))
    .filter((item) => item.text.trim().length > 0);
  const cautions = arrayOrEmpty<Record<string, unknown>>(value.cautions)
    .filter(isRecord)
    .map((item) => ({
      text: typeof item.text === "string" ? item.text : "",
      sourceUrl: safeExternalHttpUrl(item.sourceUrl)
    }))
    .filter((item) => item.text.trim().length > 0);
  const sourceRefs = arrayOrEmpty<Record<string, unknown>>(value.sourceRefs)
    .filter(isRecord)
    .map((source) => ({
      refId: typeof source.refId === "string" ? source.refId : undefined,
      sourceFingerprint: typeof source.sourceFingerprint === "string" ? source.sourceFingerprint : undefined,
      title: typeof source.title === "string" ? source.title : "",
      url: safeExternalHttpUrl(source.url) ?? "",
      sourceName: typeof source.sourceName === "string" ? source.sourceName : undefined,
      queriedAt: typeof source.queriedAt === "string" ? source.queriedAt : undefined,
      credibilityRank: typeof source.credibilityRank === "string" ? source.credibilityRank : undefined
    }))
    .filter((source) => source.url.length > 0);
  const relevance = isRecord(value.relevanceFilter) ? value.relevanceFilter : null;
  const rawReasonCounts = relevance && isRecord(relevance.reasonCounts) ? relevance.reasonCounts : {};
  const reasonCounts = Object.fromEntries(
    Object.entries(rawReasonCounts).filter(
      (entry): entry is [string, number] => typeof entry[1] === "number" && Number.isFinite(entry[1]) && entry[1] >= 0
    )
  );
  const status =
    value.status === "completed" || value.status === "no_results" || value.status === "failed"
      ? value.status
      : recommendations.length
        ? "completed"
        : "no_results";
  const normalizedRelevance = relevance
    ? {
        acceptedResultCount:
          typeof relevance.acceptedResultCount === "number"
            ? Math.max(0, Math.trunc(relevance.acceptedResultCount))
            : recommendations.length,
        rejectedResultCount:
          typeof relevance.rejectedResultCount === "number"
            ? Math.max(0, Math.trunc(relevance.rejectedResultCount))
            : 0,
        reasonCounts
      }
    : null;
  const rawConclusion = isRecord(value.conclusion) ? value.conclusion : null;
  const conclusion = rawConclusion
    ? {
        status: typeof rawConclusion.status === "string" ? rawConclusion.status : "partial",
        overview: typeof rawConclusion.overview === "string" ? rawConclusion.overview : "",
        takeaways: arrayOrEmpty<Record<string, unknown>>(rawConclusion.takeaways)
          .filter(isRecord)
          .map((item) => ({
            intentType: typeof item.intentType === "string" ? item.intentType : "general",
            themeLabel: typeof item.themeLabel === "string" ? item.themeLabel : "旅行建议",
            text: typeof item.text === "string" ? item.text : "",
            sourceRefIds: arrayOrEmpty<string>(item.sourceRefIds).filter((ref) => typeof ref === "string")
          }))
          .filter((item) => item.text.trim().length > 0),
        conflicts: arrayOrEmpty<Record<string, unknown>>(rawConclusion.conflicts)
          .filter(isRecord)
          .map((item) => ({
            topic: typeof item.topic === "string" ? item.topic : "来源冲突",
            summary: typeof item.summary === "string" ? item.summary : "",
            sourceRefIds: arrayOrEmpty<string>(item.sourceRefIds).filter((ref) => typeof ref === "string")
          }))
          .filter((item) => item.summary.trim().length > 0),
        missingThemes: arrayOrEmpty<Record<string, unknown>>(rawConclusion.missingThemes)
          .filter(isRecord)
          .map((item) => ({
            intentType: typeof item.intentType === "string" ? item.intentType : "general",
            themeLabel: typeof item.themeLabel === "string" ? item.themeLabel : "待补信息"
          })),
        evidenceBasis:
          typeof rawConclusion.evidenceBasis === "string" ? rawConclusion.evidenceBasis : "search_result_snippets",
        generationMethod:
          typeof rawConclusion.generationMethod === "string"
            ? rawConclusion.generationMethod
            : "deterministic_fallback_v1",
        fallbackReasonCode:
          typeof rawConclusion.fallbackReasonCode === "string" ? rawConclusion.fallbackReasonCode : undefined
      }
    : undefined;
  const placeHints = arrayOrEmpty<Record<string, unknown>>(value.placeHints)
    .filter(isRecord)
    .map((item) => {
      const sourceRefIds = arrayOrEmpty<unknown>(item.sourceRefIds).filter(
        (ref): ref is string => typeof ref === "string" && ref.trim().length > 0
      );
      const sourceFingerprints = arrayOrEmpty<unknown>(item.sourceFingerprints).filter(
        (fingerprint): fingerprint is string => typeof fingerprint === "string" && fingerprint.trim().length > 0
      );
      if (
        typeof item.mentionText !== "string" ||
        !item.mentionText.trim() ||
        typeof item.intentType !== "string" ||
        !item.intentType.trim() ||
        typeof item.guideEvidenceFingerprint !== "string" ||
        !item.guideEvidenceFingerprint.trim() ||
        !sourceRefIds.length ||
        !sourceFingerprints.length
      ) {
        return null;
      }
      return {
        schemaVersion: typeof item.schemaVersion === "string" ? item.schemaVersion : "guide-place-hint-v1",
        mentionText: item.mentionText.trim(),
        intentType: item.intentType.trim(),
        sourceRefIds,
        sourceFingerprints,
        guideEvidenceFingerprint: item.guideEvidenceFingerprint.trim(),
        verificationStatus:
          typeof item.verificationStatus === "string" ? item.verificationStatus : "unresolved_amap_grounding"
      } satisfies TravelGuidePlaceHint;
    })
    .filter((item): item is TravelGuidePlaceHint => item !== null);
  return {
    status,
    failureReason: typeof value.failureReason === "string" ? value.failureReason : null,
    recommendations,
    cautions,
    sourceRefs,
    queryFingerprint: typeof value.queryFingerprint === "string" ? value.queryFingerprint : "",
    evidenceFingerprint: typeof value.evidenceFingerprint === "string" ? value.evidenceFingerprint : undefined,
    queriedAt: typeof value.queriedAt === "string" ? value.queriedAt : "",
    caveat: typeof value.caveat === "string" ? value.caveat : "普通攻略只作经验性建议；新地点不会自动写入行程。",
    queryCount:
      typeof value.queryCount === "number" && Number.isFinite(value.queryCount)
        ? Math.max(0, Math.trunc(value.queryCount))
        : 0,
    ...(normalizedRelevance ? { relevanceFilter: normalizedRelevance } : {}),
    attemptedProviders: arrayOrEmpty<string>(value.attemptedProviders).filter((item) => typeof item === "string"),
    successfulProviders: arrayOrEmpty<string>(value.successfulProviders).filter((item) => typeof item === "string"),
    failedProviders: arrayOrEmpty<string>(value.failedProviders).filter((item) => typeof item === "string"),
    skippedProviders: arrayOrEmpty<string>(value.skippedProviders).filter((item) => typeof item === "string"),
    providerDiagnostics: arrayOrEmpty<Record<string, unknown>>(value.providerDiagnostics).filter(isRecord),
    ...(conclusion ? { conclusion } : {}),
    ...(placeHints.length ? { placeHints } : {})
  };
}

function normalizeSharedTravelSource(value: unknown): SharedTravelSource | null {
  if (
    !isRecord(value) || value.schemaVersion !== "shared-travel-source-v1" ||
    !["completed", "needs_user_material"].includes(String(value.status)) ||
    typeof value.sourceMaterialId !== "string" || !value.sourceMaterialId ||
    value.imageStatus !== "not_read"
  ) {
    return null;
  }
  return {
    schemaVersion: "shared-travel-source-v1",
    status: value.status as SharedTravelSource["status"],
    sourceMaterialId: value.sourceMaterialId,
    canonicalUrl: typeof value.canonicalUrl === "string" ? value.canonicalUrl : null,
    title: typeof value.title === "string" ? value.title : null,
    bodyText: typeof value.bodyText === "string" ? value.bodyText : null,
    contentFingerprint: typeof value.contentFingerprint === "string" ? value.contentFingerprint : null,
    imageCount: typeof value.imageCount === "number" && Number.isSafeInteger(value.imageCount) && value.imageCount >= 0
      ? value.imageCount : 0,
    imageStatus: "not_read",
    failureReason: typeof value.failureReason === "string" ? value.failureReason : null
  };
}

function normalizeConversationTurn(value: ConversationTurn): ConversationTurn {
  const turn = requireRecordField<ConversationTurn>(value, "conversation turn", "INVALID_AGENT_TURN_RESPONSE");
  const raw = turn as unknown as Record<string, unknown>;
  return {
    ...turn,
    guideAdvice: normalizeTravelGuideAdvice(raw.guideAdvice),
    sharedSource: normalizeSharedTravelSource(raw.sharedSource)
  };
}

function normalizeAgentSession(value: AgentSession): AgentSession {
  const session = requireRecordField<AgentSession>(value, "session", "INVALID_AGENT_SESSION_RESPONSE");
  const raw = session as unknown as Record<string, unknown>;
  return {
    ...session,
    turns: arrayOrEmpty<ConversationTurn>(raw.turns).map(normalizeConversationTurn),
    itinerary: normalizeNullableItineraryPlan(raw.itinerary),
    pendingPoiCandidates: arrayOrEmpty<PendingPoiCandidate>(raw.pendingPoiCandidates),
    planningRun: isRecord(raw.planningRun) ? (raw.planningRun as PlanningRun) : null
  };
}

function normalizeAgentSessionList(value: AgentSessionListResponse): AgentSessionListResponse {
  return {
    ...value,
    sessions: arrayOrEmpty<AgentSessionSummary>((value as unknown as Record<string, unknown>).sessions)
  };
}

function normalizeAgentMessageResponse<T extends AgentMessageResponse | AgentMessageEditResponse>(value: T): T {
  const response = requireRecordField<T>(value, "agent message", "INVALID_AGENT_MESSAGE_RESPONSE");
  const raw = response as unknown as Record<string, unknown>;
  const normalizedTurns: Record<string, unknown> = {};
  if (isRecord(raw.userTurn)) {
    normalizedTurns.userTurn = normalizeConversationTurn(raw.userTurn as unknown as ConversationTurn);
  }
  if (isRecord(raw.editedTurn)) {
    normalizedTurns.editedTurn = normalizeConversationTurn(raw.editedTurn as unknown as ConversationTurn);
  }
  if (isRecord(raw.assistantTurn)) {
    normalizedTurns.assistantTurn = normalizeConversationTurn(raw.assistantTurn as unknown as ConversationTurn);
  } else if (raw.assistantTurn === null) {
    normalizedTurns.assistantTurn = null;
  }
  return {
    ...response,
    ...normalizedTurns,
    itinerary: normalizeNullableItineraryPlan(raw.itinerary),
    pendingPoiCandidates: arrayOrEmpty<PendingPoiCandidate>(raw.pendingPoiCandidates),
    warnings: arrayOrEmpty<string>(raw.warnings),
    planningSteps: arrayOrEmpty<AgentPlanningEvent>(raw.planningSteps),
    toolEvents: arrayOrEmpty<AgentPlanningEvent>(raw.toolEvents),
    reasoningStatuses: arrayOrEmpty<AgentReasoningStatus>(raw.reasoningStatuses)
  } as T;
}

function normalizePatchResponse(value: ItineraryPatchResponse): ItineraryPatchResponse {
  const response = requireRecordField<ItineraryPatchResponse>(
    value,
    "patch response",
    "INVALID_ITINERARY_PATCH_RESPONSE"
  );
  const raw = response as unknown as Record<string, unknown>;
  const version = requireRecordField<ItineraryPatchResponse["version"]>(
    raw.version,
    "version",
    "INVALID_ITINERARY_PATCH_RESPONSE"
  );
  if (!version.id) {
    throw new ApiError("API response missing version id", { status: 0, code: "INVALID_ITINERARY_PATCH_RESPONSE" });
  }
  return {
    ...response,
    itinerary: normalizeItineraryPlan(raw.itinerary),
    version,
    validationErrors: arrayOrEmpty<string>(raw.validationErrors),
    pendingPoiCandidates: arrayOrEmpty<PendingPoiCandidate>(raw.pendingPoiCandidates)
  };
}

function normalizeMapPoiSearchResponse(value: MapPoiSearchResponse): MapPoiSearchResponse {
  return {
    ...value,
    pois: arrayOrEmpty<MapPoi>((value as unknown as Record<string, unknown>).pois)
  };
}

function normalizeMapPoiResolveResponse(value: MapPoiResolveResponse): MapPoiResolveResponse {
  const raw = value as unknown as Record<string, unknown>;
  return {
    ...value,
    resolved: arrayOrEmpty<MapPoiResolveResponse["resolved"][number]>(raw.resolved),
    pending: arrayOrEmpty<MapPoiResolveResponse["pending"][number]>(raw.pending).map((item) => ({
      ...item,
      candidates: arrayOrEmpty<MapPoi>((item as unknown as Record<string, unknown>).candidates)
    }))
  };
}

export const apiClient = {
  getProviderStatus: () => request<ProviderStatusResponse>("/providers/status"),
  listAgentSessions: () => request<AgentSessionListResponse>("/agent/sessions").then(normalizeAgentSessionList),
  createAgentSession: (payload: { city: string; title?: string; preferenceCardId?: string }) =>
    request<AgentSession>("/agent/sessions", {
      method: "POST",
      body: JSON.stringify(payload)
    }).then(normalizeAgentSession),
  getCurrentAgentSession: () => request<AgentSession>("/agent/sessions/current").then(normalizeAgentSession),
  getAgentSession: (sessionId: string) =>
    request<AgentSession>(`/agent/sessions/${sessionId}`).then(normalizeAgentSession),
  bindSpatialMapSelection: (
    sessionId: string,
    payload: {
      sourceAssistantTurnId: string;
      checkpointId: string;
      checkpointFingerprint: string;
      amapPoiId: string;
      label: string;
      radiusMeters: number;
    }
  ) =>
    request<SpatialMapSelectionBindResponse>(
      `/agent/sessions/${encodeURIComponent(sessionId)}/clarification-spatial-map-selections`,
      { method: "POST", body: JSON.stringify(payload) }
    ),
  getAgentReasoningStatuses: (sessionId: string, options: { turnId?: string; afterSequence?: number } = {}) => {
    const query = new URLSearchParams();
    if (options.turnId) {
      query.set("turnId", options.turnId);
    }
    if (typeof options.afterSequence === "number") {
      query.set("afterSequence", String(Math.max(0, options.afterSequence)));
    }
    const queryString = query.toString();
    const suffix = queryString ? `?${queryString}` : "";
    return request<AgentReasoningStatusSnapshot>(
      `/agent/sessions/${encodeURIComponent(sessionId)}/reasoning-statuses${suffix}`
    ).then((value) => ({
      ...value,
      statuses: arrayOrEmpty<AgentReasoningStatus>(value.statuses)
    }));
  },
  saveActiveDirection: (sessionId: string, proposalId: string, payload: SaveActiveDirectionRequest) =>
    request<SaveActiveDirectionResponse>(
      `/agent/sessions/${encodeURIComponent(sessionId)}/directions/${encodeURIComponent(proposalId)}/save-active`,
      {
        method: "POST",
        body: JSON.stringify(payload)
      }
    ),
  exportAgentDebugBundle: (sessionId: string) =>
    request<Record<string, unknown>>(`/agent/sessions/${sessionId}/debug-bundle`),
  exportPlanningTrace: (sessionId: string, assistantTurnId: string, planningRunId: string) =>
    request<unknown>(
      `/agent/sessions/${sessionId}/turns/${assistantTurnId}/planning-runs/${planningRunId}/trace-export`
    ).then((value) => validatePlanningTraceExport(value, { sessionId, assistantTurnId, planningRunId })),
  listSavedItineraryVersions: (planId: string) =>
    request<SavedItineraryVersionsResponse>(`/itineraries/${planId}/versions/saved`).then((value) => ({
      ...value,
      savedVersions: arrayOrEmpty<SavedItineraryVersion>((value as unknown as Record<string, unknown>).savedVersions)
    })),
  saveItineraryVersion: (planId: string, versionId: string) =>
    request<SavedItineraryVersion>(`/itineraries/${planId}/versions/${versionId}/save`, {
      method: "POST",
      body: JSON.stringify({})
    }),
  exportItineraryJson: (planId: string) =>
    request<ItineraryExportJson>(`/itineraries/${planId}/export?format=json`).then((value) => ({
      ...value,
      itineraryPlan: normalizeItineraryPlan((value as unknown as Record<string, unknown>).itineraryPlan),
      routeOptions: arrayOrEmpty<RouteOption>((value as unknown as Record<string, unknown>).routeOptions)
    })),
  exportItineraryMarkdown: (planId: string) => requestText(`/itineraries/${planId}/export?format=markdown`),
  deleteAgentSession: (sessionId: string) =>
    request<AgentSessionListResponse>(`/agent/sessions/${sessionId}`, {
      method: "DELETE"
    }).then(normalizeAgentSessionList),
  rejectPendingPoiCandidate: (sessionId: string, candidateId: string) =>
    request<AgentSession>(`/agent/sessions/${sessionId}/pending-poi-candidates/${candidateId}/reject`, {
      method: "POST",
      body: JSON.stringify({})
    }).then(normalizeAgentSession),
  sendAgentMessage: (
    sessionId: string,
    payload: {
      requestId?: string;
      content: string;
      agentModel?: string;
      context?: Record<string, unknown>;
    }
  ) =>
    request<AgentMessageResponse>(`/agent/sessions/${sessionId}/messages`, {
      method: "POST",
      body: JSON.stringify(payload)
    }).then(normalizeAgentMessageResponse),
  sendAgentMessageStream: sendAgentMessageStreamRequest,
  cancelAgentRun: (sessionId: string) =>
    request<{ cancelRequested: boolean }>(`/agent/sessions/${sessionId}/runs/cancel`, { method: "POST" }),
  refreshProposalVisitFacts: (sessionId: string, proposalId: string) =>
    request<ProposalVisitFactsRefreshResponse>(
      `/agent/sessions/${sessionId}/plan-proposals/${proposalId}/visit-facts/refresh`,
      { method: "POST", body: JSON.stringify({}) }
    ),
  editAgentMessage: (
    sessionId: string,
    turnId: string,
    payload: {
      content: string;
      agentModel?: string;
      regenerate?: boolean;
      context?: Record<string, unknown>;
    },
    options: { signal?: AbortSignal } = {}
  ) =>
    request<AgentMessageEditResponse>(`/agent/sessions/${sessionId}/messages/${turnId}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
      signal: options.signal
    }).then(normalizeAgentMessageResponse),
  resumeAgentTurn: (sessionId: string, turnId: string, options: { signal?: AbortSignal } = {}) =>
    request<AgentMessageResponse>(`/agent/sessions/${sessionId}/turns/${turnId}/resume`, {
      method: "POST",
      body: JSON.stringify({}),
      signal: options.signal
    }).then(normalizeAgentMessageResponse),
  getMapConfig: () => request<MapConfigResponse>("/map/config"),
  searchMapPois: (payload: { city: string; keyword?: string; category?: string }) => {
    const params = new URLSearchParams({
      city: payload.city,
      keyword: payload.keyword ?? "",
      category: payload.category ?? "all"
    });
    return request<MapPoiSearchResponse>(`/map/pois?${params.toString()}`).then(normalizeMapPoiSearchResponse);
  },
  searchNearbyMapPois: (payload: {
    city: string;
    longitude: number;
    latitude: number;
    keyword: string;
    category?: string;
    radius?: number;
  }) => {
    const params = new URLSearchParams({
      city: payload.city,
      longitude: String(payload.longitude),
      latitude: String(payload.latitude),
      keyword: payload.keyword,
      category: payload.category ?? "all",
      radius: String(payload.radius ?? 1500)
    });
    return request<MapPoiSearchResponse>(`/map/pois/nearby?${params.toString()}`).then(normalizeMapPoiSearchResponse);
  },
  resolveMapPois: (payload: { sessionId: string; turnId?: string; city: string; queries: MapPoiResolveQuery[] }) =>
    request<MapPoiResolveResponse>("/map/pois/resolve", {
      method: "POST",
      body: JSON.stringify(payload)
    }).then(normalizeMapPoiResolveResponse),
  uploadSourceMaterial: (file: File, kind: string, saveOriginal: boolean) => {
    const body = new FormData();
    body.append("file", file);
    body.append("kind", kind);
    body.append("saveOriginal", String(saveOriginal));
    return request<SourceMaterialUploadResponse>("/source-materials/upload", {
      method: "POST",
      body
    });
  },
  ingestSocialLink: (url: string) =>
    request<SocialLinkIngestResponse>("/source-materials/social-link", {
      method: "POST",
      body: JSON.stringify({ url })
    }),
  createInspiration: (payload: InspirationPayload) =>
    request<InspirationCreateResponse>("/inspirations", {
      method: "POST",
      body: JSON.stringify(payload)
    }),
  extractInspiration: (inspirationSetId: string) =>
    request<ExtractionResponse>(`/inspirations/${inspirationSetId}/extract`, {
      method: "POST",
      body: JSON.stringify({})
    }),
  generateItinerary: (payload: {
    inspirationSetId: string;
    city: string;
    preferenceProfileId?: string;
    preferenceSummary?: string;
    planningContext?: Record<string, unknown>;
  }) =>
    request<ItineraryPlanEnvelope>("/itineraries/generate", {
      method: "POST",
      body: JSON.stringify(payload)
    }),
  compareItineraries: (payload: {
    inspirationSetId: string;
    city: string;
    preferenceProfileId?: string;
    preferenceSummary?: string;
    planningContext?: Record<string, unknown>;
  }) =>
    request<PlanComparisonResponse>("/itineraries/compare", {
      method: "POST",
      body: JSON.stringify(payload)
    }),
  extractPreferences: (conversationText: string) =>
    request<PreferenceEnvelope>("/preferences/extract", {
      method: "POST",
      body: JSON.stringify({ conversationText })
    }),
  updatePreferenceSummary: (cardId: string, summaryText: string) =>
    request<PreferenceEnvelope>(`/preferences/${cardId}`, {
      method: "PATCH",
      body: JSON.stringify({ summaryText })
    }),
  getPreferenceMemory: (sessionId?: string | null) => {
    const params = sessionId ? `?${new URLSearchParams({ sessionId }).toString()}` : "";
    return request<PreferenceMemory>(`/preferences/memory${params}`);
  },
  updatePreferenceMemory: (
    payload: {
      memoryText?: string;
      structuredMemory?: PreferenceMemory["structuredMemory"];
      autoUpdateEnabled?: boolean;
    },
    sessionId?: string | null
  ) => {
    const params = sessionId ? `?${new URLSearchParams({ sessionId }).toString()}` : "";
    return request<PreferenceMemory>(`/preferences/memory${params}`, {
      method: "PATCH",
      body: JSON.stringify(payload)
    });
  },
  restorePreferenceMemory: (sessionId?: string | null) => {
    const params = sessionId ? `?${new URLSearchParams({ sessionId }).toString()}` : "";
    return request<PreferenceMemory>(`/preferences/memory/restore-default${params}`, {
      method: "POST",
      body: JSON.stringify({})
    });
  },
  createWeatherReminder: (payload: { itineraryPlanId: string; emailAddress: string; triggerDate: string }) =>
    request<WeatherReminderResponse>("/reminders/weather", {
      method: "POST",
      body: JSON.stringify(payload)
    }),
  cleanupOriginalImages: () =>
    request<SourceCleanupResponse>("/source-materials/cleanup-originals", {
      method: "POST",
      body: JSON.stringify({})
    }),
  refreshItineraryTickets: (planId: string) =>
    request<ItineraryPlanEnvelope>(`/itineraries/${planId}/tickets/refresh`, {
      method: "POST",
      body: JSON.stringify({})
    }),
  refreshItineraryVisitFacts: (planId: string) =>
    request<ItineraryPlanEnvelope>(`/itineraries/${planId}/visit-facts/refresh`, {
      method: "POST",
      body: JSON.stringify({})
    }),
  patchItinerary: (
    planId: string,
    payload: {
      sourceType?: string;
      baseVersionId: string | null;
      sourceTurnId?: string;
      preferenceSummary?: string;
      planningContext?: Record<string, unknown>;
      operations: ItineraryPatchOperation[];
    }
  ) =>
    request<ItineraryPatchResponse>(`/itineraries/${planId}/patch`, {
      method: "POST",
      body: JSON.stringify(payload)
    }).then(normalizePatchResponse),
  selectRoute: (
    planId: string,
    routeOptionId: string,
    payload: { baseVersionId: string | null; preferenceSummary?: string; planningContext?: Record<string, unknown> }
  ) =>
    request<ItineraryPatchResponse>(`/itineraries/${planId}/routes/${routeOptionId}/select`, {
      method: "POST",
      body: JSON.stringify(payload)
    }).then(normalizePatchResponse),
  optimizeRoutes: (
    planId: string,
    payload: {
      baseVersionId: string | null;
      preferenceSummary?: string;
      planningContext?: Record<string, unknown>;
      dayId?: string | null;
      optimizationObjective?: "balanced" | "fastest" | "cheapest";
    }
  ) =>
    request<ItineraryPatchResponse>(`/itineraries/${planId}/routes/optimize`, {
      method: "POST",
      body: JSON.stringify(payload)
    }).then(normalizePatchResponse),
  suggestLocalReplan: (
    planId: string,
    payload: { userInput?: string; preferenceSummary?: string; planningContext?: Record<string, unknown> }
  ) =>
    request<PlanningRun>(`/itineraries/${planId}/local-replan/suggestions`, {
      method: "POST",
      body: JSON.stringify(payload)
    }),
  restoreItineraryVersion: (planId: string, payload: { versionId: string; reason?: string }) =>
    request<{ itinerary: ItineraryPlan; version: { id: string; versionNumber: number; sourceType: string } }>(
      `/itineraries/${planId}/restore-version`,
      {
        method: "POST",
        body: JSON.stringify(payload)
      }
    )
};

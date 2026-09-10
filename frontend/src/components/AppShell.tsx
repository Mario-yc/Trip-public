import { Fragment, type CSSProperties, type PointerEvent, useEffect, useMemo, useRef, useState } from "react";
import { Copy, Pencil, Pin } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { AgentPlanningProcessModel } from "./agent/AgentPlanningProcess";
import { AgentReasoningProgress, aggregateStatuses } from "./agent/AgentReasoningProgress";
import { AgentClarificationBatch } from "./agent/AgentClarificationBatch";
import { TravelGuideAdviceCard } from "./agent/TravelGuideAdviceCard";
import { SharedTravelSourceCard } from "./agent/SharedTravelSourceCard";
import { isCurrentSpatialBoundaryPreviewTurn, SpatialBoundaryPreviewCard } from "./agent/SpatialBoundaryPreviewCard";
import { ExtractionReview } from "./agent/ExtractionReview";
import { InspirationInput } from "./agent/InspirationInput";
import { PlanComparison } from "./comparison/PlanComparison";
import { PlannerMap, type MapInteractionDebugState } from "./map/PlannerMap";
import {
  defaultPreferenceCard,
  effectivePreferenceMemoryText,
  PreferenceSummaryCard
} from "./preferences/PreferenceSummaryCard";
import { CostBreakdownPanel } from "./timeline/CostBreakdownPanel";
import { DailyTimeline, formatTimelineForCopy } from "./timeline/DailyTimeline";
import { ItineraryDraft } from "./timeline/ItineraryDraft";
import { WorkspacePanelSwitcher } from "./workspace/WorkspacePanelSwitcher";
import { WorkspaceToolbar } from "./workspace/WorkspaceToolbar";
import { preferredScrollBehavior, useChatFollow } from "./workspace/useChatFollow";
import { createEditableDays } from "./timeline/itineraryWorkspace";
import { isRouteAnchorSegment } from "./timeline/routeAnchors";
import {
  apiClient,
  ApiError,
  AgentPlanningEvent,
  AgentReasoningStatus,
  AgentReasoningStatusSnapshot,
  AgentChoiceOption,
  ClarificationCheckpoint,
  ClarificationBatchSelection,
  AgentSession,
  ConversationTurn,
  ExtractionResponse,
  InspirationInputPayload,
  InspirationPayload,
  ItineraryPlan,
  ItineraryPatchOperation,
  LocalReplanSuggestion,
  MapPoi,
  PendingTimelineSlot,
  ProviderStatus,
  ProviderStatusResponse
} from "../services/apiClient";
import { buildAgentContext, buildPlanningContext } from "../state/agentContext";
import { plannerStore } from "../state/plannerStore";
import {
  beginPendingSlotComparison,
  type ComparisonPlanProjection,
  comparisonPreviewFromTurns,
  comparisonProjectionFromUnknown,
  createComparisonPreviewState,
  focusComparisonPlan,
  isCurrentComparisonScope,
  markComparisonPlanAdopted,
  type PlanComparisonPreviewState,
  proposalVisitFactsNeedsAutoRefresh,
  updateComparisonPlanSnapshot,
  upsertVisibleComparisonPlan
} from "../state/planComparisonPreview";
import { isCurrentVersionedWrite } from "../state/versionedWriteGuard";
import { AGENT_MODEL_OPTIONS, normalizeAgentModel } from "../modelRegistry";
import { buildTripDebugBundleJson, buildTripTestBundle, deliverTripDebugBundle } from "../debug/tripTestBundle";

const AGENT_MODEL_STORAGE_KEY = "trip.agentModel";
const ACTIVE_AGENT_SESSION_STORAGE_KEY = "trip.activeAgentSessionId";
const AGENT_PANEL_WIDTH_STORAGE_KEY = "trip.agentPanelWidth";
const TIMELINE_PANEL_WIDTH_STORAGE_KEY = "trip.timelinePanelWidth";
const DEFAULT_AGENT_PANEL_WIDTH = 370;
const DEFAULT_TIMELINE_PANEL_WIDTH = 530;
const WORKSPACE_MAP_MIN_WIDTH = 420;
const WORKSPACE_RESIZER_WIDTH = 12;

function latestAssistantTurn(turns: ConversationTurn[]): ConversationTurn | null {
  return [...turns].reverse().find((turn) => turn.role === "assistant" && turn.status === "active") ?? null;
}

function firstPopulatedDay(plan?: ItineraryPlan | null) {
  return plan?.days.find((day) => day.segments.length > 0) ?? plan?.days[0];
}

type PendingPlanningStep = {
  type: AgentPlanningEvent["type"];
  label: string;
  status: AgentPlanningEvent["status"];
  detail: string;
  sequence: number;
  providerName?: string | null;
  fallbackUsed: boolean;
  failureReason?: string | null;
  metadata?: AgentPlanningEvent["metadata"];
  timestamp?: string;
  durationMs?: number;
  runElapsedMs?: number;
};

export function AppShell() {
  const [providerStatus, setProviderStatus] = useState<ProviderStatus[]>([]);
  const [selectedAgentModel, setSelectedAgentModel] = useState(() =>
    normalizeAgentModel(typeof window === "undefined" ? null : window.localStorage?.getItem(AGENT_MODEL_STORAGE_KEY))
  );
  const [agentProvider, setAgentProvider] = useState<ProviderStatusResponse["agent"] | null>(null);
  const [plannerSnapshot, setPlannerSnapshot] = useState(plannerStore.getSnapshot());
  const [extraction, setExtraction] = useState<ExtractionResponse | null>(null);
  const [isGenerating, setIsGenerating] = useState(false);
  const [planningProcess, setPlanningProcess] = useState<AgentPlanningProcessModel | null>(null);
  const [pendingAgentMessage, setPendingAgentMessage] = useState("");
  const [pendingPlanningStep, setPendingPlanningStep] = useState<PendingPlanningStep | null>(null);
  const [liveReasoningStatuses, setLiveReasoningStatuses] = useState<AgentReasoningStatus[]>([]);
  const [agentRunStartedAtMs, setAgentRunStartedAtMs] = useState<number | null>(null);
  const [agentRunElapsedMs, setAgentRunElapsedMs] = useState(0);
  const [errorMessage, setErrorMessage] = useState("");
  const [comparisonError, setComparisonError] = useState("");
  const [preferenceError, setPreferenceError] = useState("");
  const [preferenceExpanded, setPreferenceExpanded] = useState(false);
  const preferenceAnchorRef = useRef<HTMLDivElement | null>(null);
  const [agentCollapsed, setAgentCollapsed] = useState(false);
  const [agentWidth, setAgentWidth] = useState(() =>
    readStoredPanelWidth(AGENT_PANEL_WIDTH_STORAGE_KEY, DEFAULT_AGENT_PANEL_WIDTH, 360, 480)
  );
  const [isResizingAgent, setIsResizingAgent] = useState(false);
  const [timelineCollapsed, setTimelineCollapsed] = useState(false);
  const [timelineWidth, setTimelineWidth] = useState(() =>
    readStoredPanelWidth(TIMELINE_PANEL_WIDTH_STORAGE_KEY, DEFAULT_TIMELINE_PANEL_WIDTH, 520, 580)
  );
  const [isResizingTimeline, setIsResizingTimeline] = useState(false);
  const [activeItineraryTab, setActiveItineraryTab] = useState<"overview" | "comparison" | "costs">("overview");
  const [activeWorkspacePanel, setActiveWorkspacePanel] = useState<"agent" | "map" | "timeline">("agent");
  const [editingTurnId, setEditingTurnId] = useState<string | null>(null);
  const [editingTurnContent, setEditingTurnContent] = useState("");
  const [editingTurnSubmittingId, setEditingTurnSubmittingId] = useState<string | null>(null);
  const [messageCopyState, setMessageCopyState] = useState<{ turnId: string; status: "copied" | "failed" } | null>(
    null
  );
  const messageCopyResetTimerRef = useRef<number | null>(null);
  const [executingAgentChoiceKey, setExecutingAgentChoiceKey] = useState<string | null>(null);
  const [agentChoiceLifecycleByKey, setAgentChoiceLifecycleByKey] = useState<
    Record<string, AgentChoiceOption["lifecycle"]>
  >({});
  const [applyingSuggestionId, setApplyingSuggestionId] = useState<string | null>(null);
  const [debugCopyState, setDebugCopyState] = useState<"idle" | "copied" | "downloaded" | "failed">("idle");
  const [saveVersionState, setSaveVersionState] = useState<"idle" | "saving" | "saved" | "failed">("idle");
  const [exportState, setExportState] = useState<"idle" | "exporting" | "exported" | "failed">("idle");
  const [saveExportMenu, setSaveExportMenu] = useState<"save" | "export" | null>(null);

  useEffect(
    () => () => {
      if (messageCopyResetTimerRef.current !== null) {
        window.clearTimeout(messageCopyResetTimerRef.current);
      }
    },
    []
  );

  useEffect(() => {
    const handleEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setPreferenceExpanded(false);
    };
    window.addEventListener("keydown", handleEscape);
    return () => window.removeEventListener("keydown", handleEscape);
  }, []);

  useEffect(() => {
    if (!preferenceExpanded) return;
    const handleOutsidePointerDown = (event: globalThis.PointerEvent) => {
      if (event.target instanceof Node && !preferenceAnchorRef.current?.contains(event.target)) {
        setPreferenceExpanded(false);
      }
    };
    document.addEventListener("pointerdown", handleOutsidePointerDown, true);
    return () => document.removeEventListener("pointerdown", handleOutsidePointerDown, true);
  }, [preferenceExpanded]);
  const [savedVersionIds, setSavedVersionIds] = useState<Set<string>>(new Set());
  const generationLockRef = useRef(false);
  const agentChoiceInFlightRef = useRef<Set<string>>(new Set());
  const agentRequestSeqRef = useRef(0);
  const agentAbortControllerRef = useRef<AbortController | null>(null);
  const sessionRequestSeqRef = useRef(0);
  const reasoningRecoverySeqRef = useRef(0);
  const chatFollow = useChatFollow(
    `${isGenerating}:${pendingAgentMessage}:${pendingPlanningStep?.label}:${pendingPlanningStep?.detail}:${plannerSnapshot.conversationTurns.length}:${activeWorkspacePanel}`,
    plannerSnapshot.agentSession?.sessionId
  );
  const agentChatRef = chatFollow.ref;
  const workspaceFocusRef = useRef<HTMLElement | null>(null);
  const mapInteractionDebugRef = useRef<MapInteractionDebugState | null>(null);
  const directionSaveInFlightRef = useRef<Promise<boolean> | null>(null);
  const visitFactsAutoRefreshRef = useRef<Set<string>>(new Set());
  const proposalVisitFactsAutoRefreshRef = useRef<Set<string>>(new Set());
  const initialRestoreComparisonPreviewRef = useRef(restoreComparisonPreview);
  const initialRecoverAgentReasoningRunRef = useRef(recoverAgentReasoningRun);

  function applyVisibleComparisonProjection(value: unknown) {
    const projection = comparisonProjectionFromUnknown(value);
    if (!projection) return;
    const current = plannerStore.getSnapshot();
    const isNewSimpleDirection = Boolean(
      projection.workflowMode === "simple_direction_v1" &&
      !projection.isAdopted &&
      !current.comparisonPreview.plans.some(
        (plan) =>
          plan.proposalId === projection.proposalId &&
          plan.planningSelectionRootTurnId === projection.planningSelectionRootTurnId &&
          plan.rootPortfolioId === projection.rootPortfolioId
      )
    );
    const next = upsertVisibleComparisonPlan(current.comparisonPreview, projection);
    plannerStore.setState({ comparisonPreview: next.state });
    if (next.shouldAutoNavigate || isNewSimpleDirection) {
      const editingSimpleDirection = Boolean(
        current.comparisonPreview.mapMode === "itinerary_edit" &&
        current.comparisonPreview.plans.some(
          (plan) =>
            plan.proposalId === current.comparisonPreview.adoptedProposalId &&
            plan.workflowMode === "simple_direction_v1"
        )
      );
      // A streamed projection can arrive before its assistant turn is durable.
      // Defer an edit -> comparison transition until the final response can be
      // saved and reloaded from the authoritative session.
      if (!editingSimpleDirection) {
        void requestItineraryTabChange("comparison");
        setActiveWorkspacePanel("timeline");
      }
    }
  }

  function restoreComparisonPreview(turns: ConversationTurn[], forceNavigation = false) {
    const current = plannerStore.getSnapshot().comparisonPreview;
    const comparisonPreview = comparisonPreviewFromTurns(turns, current);
    const rootChanged = current.planningSelectionRootTurnId !== comparisonPreview.planningSelectionRootTurnId;
    const hasNewSimpleDirection = comparisonPreview.plans.some(
      (plan) =>
        plan.workflowMode === "simple_direction_v1" &&
        !plan.isAdopted &&
        !current.plans.some(
          (existing) =>
            existing.proposalId === plan.proposalId &&
            existing.planningSelectionRootTurnId === plan.planningSelectionRootTurnId &&
            existing.rootPortfolioId === plan.rootPortfolioId
        )
    );
    const shouldAutoNavigate = Boolean(
      comparisonPreview.plans.length > 0 &&
      !comparisonPreview.adoptedProposalId &&
      (rootChanged || (!current.autoNavigationCompleted && forceNavigation))
    );
    const adoptionCompleted = Boolean(
      comparisonPreview.adoptedProposalId && comparisonPreview.adoptedProposalId !== current.adoptedProposalId
    );
    plannerStore.setState({ comparisonPreview });
    if (hasNewSimpleDirection) void requestItineraryTabChange("comparison");
    else if (adoptionCompleted) setActiveItineraryTab("overview");
    else if (shouldAutoNavigate) setActiveItineraryTab("comparison");
  }
  const saveExportMenuRef = useRef<HTMLDivElement | null>(null);
  const selectedCity = plannerSnapshot.selectedCity;
  const sourceCount = extraction?.sourceLinks.length ?? providerStatus.length;
  const lastQueried = useMemo(() => new Date().toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" }), []);
  const availableAgentModels = agentProvider?.availableModels?.length
    ? agentProvider.availableModels
    : AGENT_MODEL_OPTIONS;
  const proposalVisitFactsRefreshScopeKey = plannerSnapshot.comparisonPreview.plans
    .map((plan) => `${plan.proposalId}:${plan.materialFingerprint ?? ""}`)
    .join("|");

  useEffect(() => plannerStore.subscribe((state) => setPlannerSnapshot(state)), []);

  useEffect(() => {
    document.title = "去哪玩AI 行程规划师";
  }, []);

  // Move focus only when layout has hidden its current owner. Keep all panes mounted.
  useEffect(() => {
    let frame = 0;
    function rememberFocus(event: FocusEvent) {
      if (event.target instanceof HTMLElement && event.target !== document.body) workspaceFocusRef.current = event.target;
    }
    function syncVisibleFocus() {
      window.cancelAnimationFrame(frame);
      frame = window.requestAnimationFrame(() => {
        const active = document.activeElement as HTMLElement | null;
        const focused = active === document.body ? workspaceFocusRef.current : active;
        if (!focused?.isConnected || focused.getClientRects().length) return;
        if (focused.closest(".workspace-tools")) {
          document.querySelector<HTMLButtonElement>(".workspace-tools-trigger")?.focus();
        } else if (focused.closest(".workspace-pane, .workspace-panel-switcher")) {
          const tab = document.getElementById(`workspace-panel-tab-${activeWorkspacePanel}`);
          if (tab?.getClientRects().length) tab.focus();
          else {
            const pane = focused.closest<HTMLElement>(".workspace-pane") ?? document.getElementById(`workspace-pane-${activeWorkspacePanel}`);
            Array.from(pane?.querySelectorAll<HTMLButtonElement>("button") ?? []).find((button) => button.getClientRects().length)?.focus();
          }
        }
      });
    }
    syncVisibleFocus();
    document.addEventListener("focusin", rememberFocus);
    window.addEventListener("resize", syncVisibleFocus);
    return () => { document.removeEventListener("focusin", rememberFocus); window.removeEventListener("resize", syncVisibleFocus); window.cancelAnimationFrame(frame); };
  }, [activeWorkspacePanel, agentCollapsed, timelineCollapsed]);

  useEffect(() => {
    function clampWorkspacePanels() {
      if (window.innerWidth < 1360) return;
      const available = window.innerWidth - WORKSPACE_MAP_MIN_WIDTH - WORKSPACE_RESIZER_WIDTH;
      let nextAgent = Math.min(Math.max(agentWidth, 360), 480);
      let nextTimeline = Math.min(Math.max(timelineWidth, 520), 580);
      if (nextAgent + nextTimeline > available) {
        nextAgent = Math.min(Math.max(available - nextTimeline, 360), 480);
        nextTimeline = Math.min(Math.max(available - nextAgent, 520), 580);
      }
      if (nextAgent !== agentWidth) setAgentWidth(nextAgent);
      if (nextTimeline !== timelineWidth) setTimelineWidth(nextTimeline);
    }
    clampWorkspacePanels();
    window.addEventListener("resize", clampWorkspacePanels);
    return () => window.removeEventListener("resize", clampWorkspacePanels);
  }, [agentWidth, timelineWidth]);

  useEffect(() => {
    if (!agentRunStartedAtMs || !isGenerating) {
      return undefined;
    }
    setAgentRunElapsedMs((current) => Math.max(current, Math.max(0, Date.now() - agentRunStartedAtMs)));
    const timer = window.setInterval(() => {
      setAgentRunElapsedMs((current) => Math.max(current, Math.max(0, Date.now() - agentRunStartedAtMs)));
    }, 1000);
    return () => window.clearInterval(timer);
  }, [agentRunStartedAtMs, isGenerating]);

  useEffect(() => {
    if (!saveExportMenu) {
      return undefined;
    }
    function handlePointerDown(event: globalThis.PointerEvent) {
      const target = event.target as Node | null;
      if (target && saveExportMenuRef.current?.contains(target)) {
        return;
      }
      setSaveExportMenu(null);
    }
    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        saveExportMenuRef.current?.querySelector<HTMLButtonElement>('button[aria-expanded="true"]')?.focus();
        setSaveExportMenu(null);
      }
    }
    document.addEventListener("pointerdown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [saveExportMenu]);

  useEffect(() => {
    const planId = plannerSnapshot.itineraryPlan?.id;
    if (!planId) {
      setSavedVersionIds(new Set());
      return;
    }
    let cancelled = false;
    apiClient
      .listSavedItineraryVersions(planId)
      .then((response) => {
        if (!cancelled) {
          setSavedVersionIds(
            (current) => new Set([...current, ...response.savedVersions.map((item) => item.versionId)])
          );
        }
      })
      .catch(() => {
        if (!cancelled) {
          setSavedVersionIds(new Set());
        }
      });
    return () => {
      cancelled = true;
    };
  }, [plannerSnapshot.itineraryPlan?.id]);

  useEffect(() => {
    const plan = plannerSnapshot.itineraryPlan;
    if (!plan?.id || !plan.days.some((day) => day.segments.some((segment) => Boolean(segment.poi?.amapId)))) {
      return;
    }
    const refreshKey = `${plan.id}:${plannerSnapshot.activeVersionId ?? "unversioned"}`;
    if (visitFactsAutoRefreshRef.current.has(refreshKey)) {
      return;
    }
    visitFactsAutoRefreshRef.current.add(refreshKey);
    let cancelled = false;
    apiClient
      .refreshItineraryVisitFacts(plan.id)
      .then((result) => {
        if (cancelled) return;
        const current = plannerStore.getSnapshot();
        if (current.itineraryPlan?.id !== plan.id || current.activeVersionId !== plannerSnapshot.activeVersionId) {
          return;
        }
        plannerStore.setState({
          itineraryPlan: result.plan,
          agentSession: current.agentSession ? { ...current.agentSession, itinerary: result.plan } : null
        });
      })
      .catch(() => {
        // Post-commit enrichment is deliberately non-blocking. The timeline
        // remains editable and exposes a manual refresh for failed/unknown facts.
      });
    return () => {
      cancelled = true;
    };
  }, [plannerSnapshot.activeVersionId, plannerSnapshot.itineraryPlan]);

  useEffect(() => {
    const sessionId = plannerSnapshot.agentSession?.sessionId;
    const candidates = plannerSnapshot.comparisonPreview.plans.filter(
      (plan) =>
        Boolean(sessionId && plan.proposalId && plan.materialFingerprint) &&
        isCurrentComparisonScope(plannerSnapshot.comparisonPreview, plan) &&
        proposalVisitFactsNeedsAutoRefresh(plan)
    );
    if (!sessionId || !candidates.length) return;
    const currentSessionId = sessionId;
    const pending = candidates.filter((plan) => {
      const key = `${sessionId}:${plan.proposalId}:${plan.materialFingerprint}`;
      if (proposalVisitFactsAutoRefreshRef.current.has(key)) return false;
      proposalVisitFactsAutoRefreshRef.current.add(key);
      return true;
    });
    if (!pending.length) return;
    const pendingIds = new Set(pending.map((plan) => plan.proposalId));
    const current = plannerStore.getSnapshot();
    plannerStore.setState({
      comparisonPreview: {
        ...current.comparisonPreview,
        plans: current.comparisonPreview.plans.map((plan) =>
          pendingIds.has(plan.proposalId) ? { ...plan, openingFactsRefreshStatus: "refreshing" } : plan
        )
      }
    });
    const queue = [...pending];
    async function worker() {
      while (true) {
        const plan = queue.shift();
        if (!plan) return;
        try {
          const result = await apiClient.refreshProposalVisitFacts(currentSessionId, plan.proposalId);
          if (result.materialFingerprint !== plan.materialFingerprint) continue;
          const latest = plannerStore.getSnapshot();
          if (latest.agentSession?.sessionId !== currentSessionId) continue;
          plannerStore.setState({
            comparisonPreview: {
              ...latest.comparisonPreview,
              plans: latest.comparisonPreview.plans.map((item) =>
                item.proposalId === plan.proposalId && item.materialFingerprint === result.materialFingerprint
                  ? {
                      ...item,
                      openingFactsRefreshStatus: result.refreshStatus,
                      visitFactsBySegment: result.visitFactsBySegment,
                      verifiedScheduleConflicts: result.verifiedScheduleConflicts
                    }
                  : item
              )
            }
          });
        } catch {
          const latest = plannerStore.getSnapshot();
          if (latest.agentSession?.sessionId !== currentSessionId) continue;
          plannerStore.setState({
            comparisonPreview: {
              ...latest.comparisonPreview,
              plans: latest.comparisonPreview.plans.map((item) =>
                item.proposalId === plan.proposalId ? { ...item, openingFactsRefreshStatus: "failed" } : item
              )
            }
          });
        }
      }
    }
    void Promise.all([worker(), worker()]);
  }, [plannerSnapshot.agentSession?.sessionId, plannerSnapshot.comparisonPreview, proposalVisitFactsRefreshScopeKey]);

  useEffect(() => {
    window.localStorage?.setItem(AGENT_MODEL_STORAGE_KEY, selectedAgentModel);
  }, [selectedAgentModel]);

  useEffect(() => {
    persistPanelWidth(AGENT_PANEL_WIDTH_STORAGE_KEY, agentWidth);
  }, [agentWidth]);

  useEffect(() => {
    persistPanelWidth(TIMELINE_PANEL_WIDTH_STORAGE_KEY, timelineWidth);
  }, [timelineWidth]);

  useEffect(() => {
    const sessionRequestSeq = sessionRequestSeqRef.current;
    const agentRequestSeq = agentRequestSeqRef.current;
    apiClient
      .listAgentSessions()
      .then((result) => {
        if (sessionRequestSeqRef.current === sessionRequestSeq && agentRequestSeqRef.current === agentRequestSeq) {
          plannerStore.setState({ agentSessions: result.sessions });
        }
      })
      .catch(() => {
        if (sessionRequestSeqRef.current === sessionRequestSeq && agentRequestSeqRef.current === agentRequestSeq) {
          plannerStore.setState({ agentSessions: [] });
        }
      });
    const storedSessionId = readStoredActiveAgentSessionId();
    const sessionRequest = storedSessionId
      ? apiClient.getAgentSession(storedSessionId).catch(() => apiClient.getCurrentAgentSession())
      : apiClient.getCurrentAgentSession();
    sessionRequest
      .then((session) => {
        if (!session?.sessionId) {
          return;
        }
        const currentSnapshot = plannerStore.getSnapshot();
        if (
          generationLockRef.current ||
          currentSnapshot.agentSession ||
          sessionRequestSeqRef.current !== sessionRequestSeq ||
          agentRequestSeqRef.current !== agentRequestSeq
        ) {
          return;
        }
        rememberActiveAgentSession(session.sessionId);
        const preferenceMemory = session.preferenceMemory ?? null;
        const firstDay = firstPopulatedDay(session.itinerary);
        const firstSegment = firstDay?.segments[0] ?? null;
        plannerStore.setState({
          agentSession: session,
          agentSessions: upsertAgentSessionSummary(currentSnapshot.agentSessions ?? [], session),
          conversationTurns: session.turns,
          activeVersionId: session.activeVersionId ?? null,
          itineraryPlan: session.itinerary,
          pendingPoiCandidates: session.pendingPoiCandidates,
          selectedCity: session.city,
          preferenceMemory,
          preferenceCard: {
            ...(plannerStore.getSnapshot().preferenceCard ?? defaultPreferenceCard()),
            summaryText: preferenceMemory?.memoryText ?? ""
          },
          selectedDayNumber: firstDay?.dayNumber ?? 1,
          selectedSegmentId: firstSegment?.id ?? null,
          selectedRouteOptionId: firstSegment
            ? (selectedRouteForSegment(session.itinerary, firstSegment.id)?.id ?? null)
            : null,
          routeWarnings: session.itinerary?.routeWarnings ?? [],
          lastPlanningRun: session.planningRun ?? null
        });
        initialRestoreComparisonPreviewRef.current(session.turns, true);
        void initialRecoverAgentReasoningRunRef.current(session.sessionId).catch(() => undefined);
      })
      .catch(() => {
        // No active session yet; the first Agent message will create one.
        if (sessionRequestSeqRef.current === sessionRequestSeq && agentRequestSeqRef.current === agentRequestSeq) {
          plannerStore.setState({ comparisonPreview: createComparisonPreviewState() });
        }
      });
    apiClient
      .getProviderStatus()
      .then((result) => {
        setProviderStatus([...result.default, ...result.mock]);
        setAgentProvider(result.agent ?? null);
      })
      .catch(() => {
        setProviderStatus([]);
        setAgentProvider(null);
      });
  }, []);

  function applyAgentSession(session: AgentSession) {
    rememberActiveAgentSession(session.sessionId);
    const preferenceMemory = session.preferenceMemory ?? null;
    const firstDay = firstPopulatedDay(session.itinerary);
    const firstSegment = firstDay?.segments[0] ?? null;
    const currentSnapshot = plannerStore.getSnapshot();
    plannerStore.setState({
      agentSession: session,
      agentSessions: upsertAgentSessionSummary(currentSnapshot.agentSessions ?? [], session),
      conversationTurns: session.turns,
      activeVersionId: session.activeVersionId ?? null,
      itineraryPlan: session.itinerary,
      pendingPoiCandidates: session.pendingPoiCandidates,
      activeDensityMapComparison: null,
      candidateMapPois: [],
      selectedMapPoi: null,
      poiSelectionStatuses: {},
      selectedCity: session.city,
      preferenceMemory,
      preferenceCard: {
        ...(plannerStore.getSnapshot().preferenceCard ?? defaultPreferenceCard()),
        summaryText: preferenceMemory?.memoryText ?? ""
      },
      selectedDayNumber: firstDay?.dayNumber ?? 1,
      selectedSegmentId: firstSegment?.id ?? null,
      selectedRouteOptionId: firstSegment
        ? (selectedRouteForSegment(session.itinerary, firstSegment.id)?.id ?? null)
        : null,
      previewRouteOptionId: null,
      routeWarnings: session.itinerary?.routeWarnings ?? [],
      lastPlanningRun: session.planningRun ?? null
    });
    restoreComparisonPreview(session.turns, true);
  }

  async function recoverAgentReasoningRun(
    sessionId: string,
    sourceUserTurnId = ""
  ): Promise<{ session: AgentSession; snapshot: AgentReasoningStatusSnapshot } | null> {
    if (plannerStore.getSnapshot().agentSession?.sessionId !== sessionId) {
      return null;
    }
    const recoverySeq = ++reasoningRecoverySeqRef.current;
    const agentRequestSeq = agentRequestSeqRef.current;
    const isCurrentRecovery = () =>
      reasoningRecoverySeqRef.current === recoverySeq &&
      isCurrentAgentRequest(agentRequestSeqRef, agentRequestSeq, sessionId);
    const ownedGenerationLock = !generationLockRef.current;
    let cursor = 0;
    let recoveryTurnId = sourceUserTurnId;
    let activeObserved = false;
    let collected: AgentReasoningStatus[] = [];
    try {
      for (let attempt = 0; attempt < 120; attempt += 1) {
        const snapshot = await apiClient.getAgentReasoningStatuses(sessionId, {
          ...(recoveryTurnId ? { turnId: recoveryTurnId } : {}),
          afterSequence: cursor
        });
        if (!isCurrentRecovery() || snapshot.sessionId !== sessionId) {
          return null;
        }
        const snapshotTurnId = snapshot.sourceUserTurnId ?? "";
        if (snapshotTurnId && recoveryTurnId && snapshotTurnId !== recoveryTurnId) {
          recoveryTurnId = snapshotTurnId;
          cursor = 0;
          collected = [];
          continue;
        }
        if (snapshotTurnId && !recoveryTurnId) {
          recoveryTurnId = snapshotTurnId;
        }
        for (const status of snapshot.statuses) {
          collected = upsertReasoningStatus(collected, status);
        }
        cursor = Math.max(cursor, snapshot.nextSequence);
        if (collected.length) {
          setLiveReasoningStatuses(collected);
        }
        if (snapshot.active) {
          activeObserved = true;
          if (ownedGenerationLock) {
            generationLockRef.current = true;
            setIsGenerating(true);
          }
          await waitForReasoningPoll(500);
          continue;
        }
        if (!recoveryTurnId && !activeObserved) {
          return null;
        }
        if (!snapshot.terminalStatus && !activeObserved) {
          return null;
        }
        const refreshed = await apiClient.getAgentSession(sessionId);
        if (!isCurrentRecovery() || refreshed.sessionId !== sessionId) {
          return null;
        }
        applyAgentSession(refreshed);
        setPlanningProcess(null);
        setPendingAgentMessage("");
        setPendingPlanningStep(null);
        setLiveReasoningStatuses([]);
        return { session: refreshed, snapshot };
      }
      throw new ApiError("Agent 处理仍在进行，已保留恢复游标，请稍后重新打开当前会话。", {
        status: 0,
        code: "REASONING_RECOVERY_TIMEOUT"
      });
    } finally {
      if (ownedGenerationLock && isCurrentRecovery()) {
        generationLockRef.current = false;
        setIsGenerating(false);
      }
    }
  }

  function clearAgentSessionTransientState() {
    // A previous request may still finish after cancellation. Invalidate its
    // callbacks as well as the local progress that belongs to the old session.
    agentRequestSeqRef.current += 1;
    reasoningRecoverySeqRef.current += 1;
    agentAbortControllerRef.current?.abort();
    agentAbortControllerRef.current = null;
    setPendingAgentMessage("");
    setPendingPlanningStep(null);
    setLiveReasoningStatuses([]);
    setAgentRunStartedAtMs(null);
    setAgentRunElapsedMs(0);
    setPlanningProcess(null);
    setExtraction(null);
    setEditingTurnId(null);
    setEditingTurnContent("");
    setEditingTurnSubmittingId(null);
  }

  async function handleSwitchAgentSession(sessionId: string) {
    if (isGenerating || !sessionId || plannerSnapshot.agentSession?.sessionId === sessionId) {
      return;
    }
    clearAgentSessionTransientState();
    const requestSeq = ++sessionRequestSeqRef.current;
    setErrorMessage("");
    try {
      const session = await apiClient.getAgentSession(sessionId);
      if (!isCurrentSessionRequest(sessionRequestSeqRef, requestSeq)) {
        return;
      }
      applyAgentSession(session);
    } catch (error) {
      if (isCurrentSessionRequest(sessionRequestSeqRef, requestSeq)) {
        setErrorMessage(error instanceof Error ? error.message : "切换对话失败，请稍后重试。");
      }
    }
  }

  async function handleCreateAgentSession() {
    if (isGenerating) {
      return;
    }
    clearAgentSessionTransientState();
    const requestSeq = ++sessionRequestSeqRef.current;
    setErrorMessage("");
    try {
      const session = await apiClient.createAgentSession({
        city: selectedCity,
        title: `${selectedCity} AI 行程`
      });
      const list = await apiClient.listAgentSessions();
      if (!isCurrentSessionRequest(sessionRequestSeqRef, requestSeq)) {
        return;
      }
      plannerStore.setState({ agentSessions: list.sessions });
      applyAgentSession(session);
    } catch (error) {
      if (isCurrentSessionRequest(sessionRequestSeqRef, requestSeq)) {
        setErrorMessage(error instanceof Error ? error.message : "新建对话失败，请稍后重试。");
      }
    }
  }

  async function handleDeleteAgentSession() {
    const current = plannerSnapshot.agentSession;
    if (!current || isGenerating) {
      return;
    }
    clearAgentSessionTransientState();
    const requestSeq = ++sessionRequestSeqRef.current;
    setErrorMessage("");
    try {
      const list = await apiClient.deleteAgentSession(current.sessionId);
      if (!isCurrentSessionRequest(sessionRequestSeqRef, requestSeq)) {
        return;
      }
      rememberActiveAgentSession(null);
      plannerStore.setState({
        agentSessions: list.sessions,
        agentSession: null,
        conversationTurns: [],
        activeVersionId: null,
        itineraryPlan: null,
        lastPlanningRun: null,
        comparisonPreview: createComparisonPreviewState(),
        itineraryAgentContext: null,
        timelineCopyText: "",
        pendingPoiCandidates: [],
        activeDensityMapComparison: null,
        candidateMapPois: [],
        selectedMapPoi: null,
        poiSelectionStatuses: {},
        selectedDayNumber: 1,
        selectedSegmentId: null,
        selectedRouteOptionId: null,
        previewRouteOptionId: null,
        routeWarnings: [],
        preferenceMemory: null,
        preferenceCard: defaultPreferenceCard()
      });
    } catch (error) {
      if (isCurrentSessionRequest(sessionRequestSeqRef, requestSeq)) {
        setErrorMessage(error instanceof Error ? error.message : "删除对话失败，请稍后重试。");
      }
    }
  }

  async function handleInspirationSubmit(payload: InspirationInputPayload): Promise<boolean> {
    if (generationLockRef.current) {
      return false;
    }
    if (!payload.files.length) {
      return handleAgentMessageSubmit(payload);
    }
    if (payload.socialLinks.length || payload.textItems.some((text) => /https?:\/\//i.test(text))) {
      setErrorMessage("链接和文件请分开发送；本次未提交，也未修改当前行程。");
      return false;
    }

    const conversationText = [...payload.textItems, ...payload.socialLinks].join("\n");
    let preferenceCard = plannerStore.getSnapshot().preferenceCard ?? defaultPreferenceCard();
    plannerStore.setState({
      selectedCity: payload.cityHint,
      itineraryPlan: null,
      itineraryAgentContext: null,
      timelineCopyText: "",
      planComparison: null,
      preferenceCard,
      selectedDayNumber: 1,
      selectedSegmentId: null,
      selectedRouteOptionId: null,
      previewRouteOptionId: null,
      routeWarnings: []
    });
    setIsGenerating(true);
    generationLockRef.current = true;
    setErrorMessage("");
    setComparisonError("");
    setPreferenceError("");
    const visibleProcess = buildVisiblePlanningProcess({
      userMessage: conversationText || "用户上传了旅行灵感素材。",
      snapshot: plannerStore.getSnapshot(),
      preferenceCard,
      city: payload.cityHint,
      finalResult: "正在生成可编辑行程、路线和方案比较。"
    });
    setPlanningProcess(visibleProcess);
    try {
      if (conversationText.trim()) {
        try {
          const preferenceResult = await apiClient.extractPreferences(conversationText);
          if (hasActualPreferenceCard(preferenceResult.summaryCard)) {
            preferenceCard = preferenceResult.summaryCard;
            plannerStore.setState({ preferenceCard });
          }
        } catch (error) {
          setPreferenceError(error instanceof Error ? error.message : "偏好自动更新失败，已继续使用当前偏好。");
        }
      }
      const requestSnapshot = { ...plannerStore.getSnapshot(), preferenceCard };
      const uploadedMaterials = await Promise.all(
        payload.files.map((file) =>
          apiClient.uploadSourceMaterial(file, payload.sourceKind, payload.saveOriginalImages)
        )
      );
      const createPayload: InspirationPayload = {
        cityHint: payload.cityHint,
        textItems: payload.textItems,
        socialLinks: payload.socialLinks,
        sourceMaterialIds: uploadedMaterials.map((material) => material.sourceMaterialId),
        saveOriginalImages: payload.saveOriginalImages,
        planningContext: buildPlanningContext(requestSnapshot)
      };
      const created = await apiClient.createInspiration(createPayload);
      const extracted = await apiClient.extractInspiration(created.inspirationSetId);
      setExtraction(extracted);
      const generated = await apiClient.generateItinerary({
        inspirationSetId: created.inspirationSetId,
        city: extracted.cityCandidates[0] ?? payload.cityHint,
        preferenceProfileId: preferenceCard.profileId,
        preferenceSummary: effectivePreferenceMemoryText(preferenceCard.summaryText),
        planningContext: buildPlanningContext(requestSnapshot)
      });
      const firstDay = firstPopulatedDay(generated.plan);
      const firstSegmentId = firstDay?.segments[0]?.id ?? null;
      const firstRoute = selectedRouteForSegment(generated.plan, firstSegmentId);
      plannerStore.setState({
        selectedCity: generated.plan.city,
        itineraryPlan: generated.plan,
        lastPlanningRun: generated.planningRun ?? null,
        selectedDayNumber: firstDay?.dayNumber ?? 1,
        selectedSegmentId: firstSegmentId,
        selectedRouteOptionId: firstRoute?.id ?? null,
        previewRouteOptionId: null,
        routeWarnings: generated.plan.routeWarnings ?? []
      });
      let comparisonFallback = false;
      try {
        const comparison = await apiClient.compareItineraries({
          inspirationSetId: created.inspirationSetId,
          city: generated.plan.city,
          preferenceProfileId: preferenceCard.profileId,
          preferenceSummary: effectivePreferenceMemoryText(preferenceCard.summaryText),
          planningContext: buildPlanningContext(plannerStore.getSnapshot())
        });
        plannerStore.setState({
          planComparison: comparison,
          lastPlanningRun: comparison.planningRun ?? generated.planningRun ?? plannerStore.getSnapshot().lastPlanningRun
        });
      } catch (comparisonError) {
        comparisonFallback = true;
        setComparisonError(
          comparisonError instanceof Error ? comparisonError.message : "多个方案比较暂不可用，行程仍可查看。"
        );
      }
      setPlanningProcess(
        buildVisiblePlanningProcess({
          userMessage: conversationText || "用户上传了旅行灵感素材。",
          snapshot: plannerStore.getSnapshot(),
          preferenceCard,
          city: generated.plan.city,
          finalResult: `已生成 ${generated.plan.days.length} 天可编辑行程，包含路线、风险和辅助方案状态。`,
          completed: true,
          comparisonFallback
        })
      );
    } catch (error) {
      setPlanningProcess(null);
      setErrorMessage(error instanceof Error ? error.message : "生成失败，请稍后重试");
      return false;
    } finally {
      setIsGenerating(false);
      generationLockRef.current = false;
    }
    return true;
  }

  async function handleAgentMessageSubmit(payload: InspirationInputPayload): Promise<boolean> {
    if (generationLockRef.current) {
      return false;
    }
    const textContent = payload.textItems.join("\n").trim();
    const embeddedLinks = new Set(textContent.match(/https?:\/\/[^\s]+/g) ?? []);
    const additionalLinks = [...new Set(payload.socialLinks.map((link) => link.trim()))]
      .filter((link) => link && link !== textContent && !embeddedLinks.has(link));
    const content = [textContent, ...additionalLinks].filter(Boolean).join("\n");
    if (!content) {
      setErrorMessage("请输入旅行需求。");
      return false;
    }
    let preferenceCard = plannerStore.getSnapshot().preferenceCard ?? defaultPreferenceCard();
    let extractedPreferenceCard: typeof preferenceCard | null = null;
    plannerStore.setState({
      selectedCity: payload.cityHint,
      preferenceCard,
      selectedDayNumber: plannerSnapshot.selectedDayNumber,
      selectedSegmentId: plannerSnapshot.selectedSegmentId
    });
    setIsGenerating(true);
    setPendingAgentMessage(content);
    setLiveReasoningStatuses([]);
    const skipSyntheticChoicePreferenceExtraction = Boolean(
      payload.selectedAgentChoice && !payload.selectedAgentChoice.manualValue
    );
    setPendingPlanningStep(null);
    generationLockRef.current = true;
    const requestSeq = ++agentRequestSeqRef.current;
    reasoningRecoverySeqRef.current += 1;
    sessionRequestSeqRef.current += 1;
    setErrorMessage("");
    setComparisonError("");
    setPreferenceError("");
    setExtraction(null);
    const visibleProcess = buildVisiblePlanningProcess({
      userMessage: content,
      snapshot: plannerStore.getSnapshot(),
      preferenceCard,
      city: payload.cityHint,
      finalResult: "正在等待 Agent 生成最终行程结果。"
    });
    setPlanningProcess(visibleProcess);
    let selectedChoiceSessionId: string | undefined;
    try {
      if (!skipSyntheticChoicePreferenceExtraction) {
        try {
          const preferenceResult = await apiClient.extractPreferences(content);
          if (agentRequestSeqRef.current !== requestSeq) return false;
          if (hasActualPreferenceCard(preferenceResult.summaryCard)) {
            preferenceCard = preferenceResult.summaryCard;
            extractedPreferenceCard = preferenceResult.summaryCard;
            plannerStore.setState({ preferenceCard });
          }
        } catch (error) {
          if (agentRequestSeqRef.current !== requestSeq) return false;
          setPreferenceError(error instanceof Error ? error.message : "偏好自动更新失败，已继续使用当前偏好。");
        }
      }
      const snapshot = { ...plannerStore.getSnapshot(), preferenceCard, selectedCity: payload.cityHint };
      const canReuseCurrentSession = Boolean(snapshot.agentSession && snapshot.agentSession.city === payload.cityHint);
      const session = canReuseCurrentSession
        ? snapshot.agentSession!
        : await apiClient.createAgentSession({
            city: payload.cityHint,
            title: `${payload.cityHint} AI 行程`,
            preferenceCardId: extractedPreferenceCard?.id
          });
      if (agentRequestSeqRef.current !== requestSeq) return false;
      selectedChoiceSessionId = session.sessionId;
      const newSessionPreferenceMemory = session.preferenceMemory ?? null;
      const newSessionPreferenceCard = extractedPreferenceCard ?? {
        ...defaultPreferenceCard(),
        summaryText: newSessionPreferenceMemory?.memoryText ?? ""
      };
      const sessionSnapshot = canReuseCurrentSession
        ? {
            ...snapshot,
            agentSession: {
              ...session,
              activeVersionId: snapshot.activeVersionId ?? session.activeVersionId,
              turns: snapshot.conversationTurns,
              itinerary: snapshot.itineraryPlan,
              pendingPoiCandidates: snapshot.pendingPoiCandidates
            },
            agentSessions: upsertAgentSessionSummary(snapshot.agentSessions ?? [], session)
          }
        : {
            ...snapshot,
            agentSession: session,
            agentSessions: upsertAgentSessionSummary(snapshot.agentSessions ?? [], session),
            activeVersionId: session.activeVersionId ?? null,
            conversationTurns: session.turns,
            pendingPoiCandidates: session.pendingPoiCandidates,
            preferenceMemory: newSessionPreferenceMemory,
            preferenceCard: newSessionPreferenceCard,
            itineraryPlan: session.itinerary,
            itineraryAgentContext: null,
            timelineCopyText: "",
            selectedDayNumber: firstPopulatedDay(session.itinerary)?.dayNumber ?? 1,
            selectedSegmentId: firstPopulatedDay(session.itinerary)?.segments[0]?.id ?? null,
            selectedRouteOptionId: null,
            previewRouteOptionId: null,
            candidateMapPois: [],
            selectedMapPoi: null,
            poiSelectionStatuses: {}
          };
      const contextPreferenceCard = canReuseCurrentSession ? preferenceCard : newSessionPreferenceCard;
      rememberActiveAgentSession(session.sessionId);
      plannerStore.setState({
        agentSession: session,
        agentSessions: sessionSnapshot.agentSessions,
        activeVersionId: sessionSnapshot.activeVersionId,
        conversationTurns: sessionSnapshot.conversationTurns,
        pendingPoiCandidates: sessionSnapshot.pendingPoiCandidates,
        itineraryPlan: sessionSnapshot.itineraryPlan,
        itineraryAgentContext: sessionSnapshot.itineraryAgentContext,
        selectedDayNumber: sessionSnapshot.selectedDayNumber,
        selectedSegmentId: sessionSnapshot.selectedSegmentId,
        selectedRouteOptionId: sessionSnapshot.selectedRouteOptionId,
        previewRouteOptionId: sessionSnapshot.previewRouteOptionId,
        candidateMapPois: canReuseCurrentSession ? snapshot.candidateMapPois : sessionSnapshot.candidateMapPois,
        selectedMapPoi: canReuseCurrentSession ? snapshot.selectedMapPoi : sessionSnapshot.selectedMapPoi,
        poiSelectionStatuses: canReuseCurrentSession
          ? snapshot.poiSelectionStatuses
          : sessionSnapshot.poiSelectionStatuses,
        preferenceMemory: sessionSnapshot.preferenceMemory,
        preferenceCard: sessionSnapshot.preferenceCard
      });
      setAgentRunStartedAtMs(Date.now());
      setAgentRunElapsedMs(0);
      const agentContext = {
        ...buildAgentContext(sessionSnapshot, contextPreferenceCard, content, {
          activeView: activeItineraryTab === "comparison" ? "comparison" : "overview"
        }),
        ...(payload.retryCandidateHints ? { retryCandidateHints: true } : {}),
        ...(payload.manualCandidateHints?.length ? { manualCandidateHints: payload.manualCandidateHints } : {}),
        ...(payload.selectedAgentChoice ? { selectedAgentChoice: payload.selectedAgentChoice } : {})
      };
      const messagePayload = {
        requestId: crypto.randomUUID(),
        content,
        agentModel: selectedAgentModel,
        context: agentContext
      };
      const abortController = new AbortController();
      agentAbortControllerRef.current = abortController;
      let streamedUserTurnId = "";
      let response: Awaited<ReturnType<typeof apiClient.sendAgentMessage>>;
      try {
        response = await apiClient.sendAgentMessageStream(session.sessionId, messagePayload, {
          onExecutionEvent: (event) => {
            if (!isCurrentAgentRequest(agentRequestSeqRef, requestSeq, session.sessionId)) {
              return;
            }
            const serverElapsed = eventRunElapsedMs(event);
            if (typeof serverElapsed === "number") {
              setAgentRunStartedAtMs(Date.now() - serverElapsed);
              setAgentRunElapsedMs((current) => Math.max(current, serverElapsed));
            }
            setPendingPlanningStep((current) => pendingStepFromExecutionEvent(event, current));
            applyVisibleComparisonProjection(event.metadata?.comparisonProjection);
          },
          onReasoningStatus: (status) => {
            if (!isCurrentAgentRequest(agentRequestSeqRef, requestSeq, session.sessionId)) {
              return;
            }
            setLiveReasoningStatuses((current) => upsertReasoningStatus(current, status));
          },
          onUserTurn: (turn) => {
            if (!isCurrentAgentRequest(agentRequestSeqRef, requestSeq, session.sessionId)) {
              return;
            }
            plannerStore.setState({
              conversationTurns: upsertConversationTurn(plannerStore.getSnapshot().conversationTurns, turn)
            });
            streamedUserTurnId = turn.id;
            setPendingAgentMessage("");
          },
          signal: abortController.signal
        });
      } catch (streamError) {
        if (!isCurrentAgentRequest(agentRequestSeqRef, requestSeq, session.sessionId)) {
          return false;
        }
        if (streamError instanceof ApiError && ["HTTP_404", "HTTP_405"].includes(streamError.code)) {
          response = await apiClient.sendAgentMessage(session.sessionId, messagePayload);
        } else if (isRecoverableAgentStreamError(streamError)) {
          const recovered = await recoverAgentReasoningRun(session.sessionId, streamedUserTurnId);
          if (!isCurrentAgentRequest(agentRequestSeqRef, requestSeq, session.sessionId)) {
            return false;
          }
          if (!recovered) {
            throw streamError;
          }
          if (recovered.snapshot.terminalStatus === "failed") {
            setErrorMessage("Agent 本轮处理未完成，已从服务器恢复失败状态；没有重复发送请求。");
            return false;
          }
          if (recovered.snapshot.terminalStatus === "cancelled") {
            setErrorMessage("Agent 本轮处理已停止，已从服务器恢复取消状态；没有重复发送请求。");
            return false;
          }
          return true;
        } else {
          throw streamError;
        }
      }
      if (!isCurrentAgentRequest(agentRequestSeqRef, requestSeq, session.sessionId)) {
        return false;
      }
      if (response.requestReplay?.isHistorical) {
        setLiveReasoningStatuses([]);
        setPendingAgentMessage("");
        setPlanningProcess(null);
        setErrorMessage("已返回原请求结果；当前会话已继续推进，未覆盖当前行程或恢复旧操作。");
        return true;
      }
      applyStructuredChoiceLifecycle(response.userTurn.structuredChoiceTrace);
      plannerStore.setState({
        conversationTurns: upsertConversationTurn(plannerStore.getSnapshot().conversationTurns, response.userTurn)
      });
      // The NDJSON endpoint streams planning events, not model text deltas.
      // Commit the authoritative assistant turn atomically instead of replaying
      // a completed response through a synthetic typewriter animation.
      if (!isCurrentAgentRequest(agentRequestSeqRef, requestSeq, session.sessionId)) {
        return false;
      }
      const nextTurns = upsertConversationTurn(plannerStore.getSnapshot().conversationTurns, response.assistantTurn);
      const nextItinerary = response.itinerary ?? session.itinerary ?? snapshot.itineraryPlan;
      const firstDay = firstPopulatedDay(nextItinerary);
      const responsePreferenceMemory =
        response.preferenceMemory ?? plannerStore.getSnapshot().preferenceMemory ?? session.preferenceMemory ?? null;
      const responsePreferenceCard = responsePreferenceMemory
        ? {
            ...(plannerStore.getSnapshot().preferenceCard ?? contextPreferenceCard ?? defaultPreferenceCard()),
            summaryText: responsePreferenceMemory.memoryText
          }
        : plannerStore.getSnapshot().preferenceCard;
      plannerStore.setState({
        agentSession: {
          ...session,
          activeVersionId: response.version?.id ?? session.activeVersionId,
          turns: nextTurns,
          itinerary: nextItinerary,
          pendingPoiCandidates: response.pendingPoiCandidates,
          preferenceMemory: responsePreferenceMemory
        },
        conversationTurns: nextTurns,
        activeVersionId: response.version?.id ?? session.activeVersionId ?? null,
        itineraryPlan: nextItinerary,
        pendingPoiCandidates: response.pendingPoiCandidates,
        activeDensityMapComparison: null,
        candidateMapPois: [],
        selectedMapPoi: null,
        selectedDayNumber: firstDay?.dayNumber ?? snapshot.selectedDayNumber,
        selectedSegmentId: firstDay?.segments[0]?.id ?? snapshot.selectedSegmentId,
        selectedRouteOptionId:
          selectedRouteForSegment(nextItinerary, firstDay?.segments[0]?.id ?? snapshot.selectedSegmentId)?.id ?? null,
        previewRouteOptionId: null,
        routeWarnings: response.itinerary?.routeWarnings ?? snapshot.routeWarnings,
        preferenceMemory: responsePreferenceMemory,
        preferenceCard: responsePreferenceCard,
        lastPlanningRun: response.planningRun ?? snapshot.lastPlanningRun
      });
      restoreComparisonPreview(nextTurns);
      setPlanningProcess(null);
      setPendingAgentMessage("");
      setPendingPlanningStep(null);
      setLiveReasoningStatuses([]);
      setAgentRunStartedAtMs(null);
      setAgentRunElapsedMs(0);
      if (response.assistantTurn.status === "failed") {
        setErrorMessage(friendlyAgentError(response.warnings[0] ?? "Agent 输出未通过校验，行程未更新。"));
        return false;
      }
      if (
        !response.version &&
        response.warnings.some(
          (warning) => warning.includes("缺少候选语义提示") || warning.includes("semantic_candidate_hint_missing")
        )
      ) {
        setErrorMessage("规划预览已生成，但尚未创建正式时间轴。缺少候选语义提示。");
        return false;
      }
      if (!response.version && response.warnings.some(isMapProviderRateLimitedText)) {
        setErrorMessage("地图服务暂时限流，已保留本轮需求和规划预览。稍后点击继续即可重试，不需要重新说明。");
        return false;
      }
      if (
        !response.version &&
        response.warnings.some(
          (warning) => warning.includes("暂未生成可执行行程") || warning.includes("地点候选未完成")
        )
      ) {
        setErrorMessage("规划预览已生成，但尚未创建正式时间轴。地点未完成，稍后重试。");
        return false;
      }
    } catch (error) {
      // A user cancellation or superseding request has already closed this
      // run. Its late AbortError must not overwrite the cancelled disclosure
      // with a raw transport failure.
      if (agentRequestSeqRef.current !== requestSeq) {
        return false;
      }
      setPlanningProcess(null);
      setPendingAgentMessage("");
      setPendingPlanningStep(null);
      setAgentRunStartedAtMs(null);
      setAgentRunElapsedMs(0);
      setLiveReasoningStatuses((current) =>
        closeReasoningStatuses(
          current,
          "failed",
          selectedChoiceSessionId ?? plannerStore.getSnapshot().agentSession?.sessionId ?? `request-${requestSeq}`,
          agentRunElapsedMs
        )
      );
      if (isPortfolioRouteQualityError(error)) {
        // A route-quality rejection is retryable, but it is not a stale-choice
        // error.  Keep the stored portfolio buttons and do not enter map POI
        // selection mode as a side effect of the failed commit.
        plannerStore.setState({
          activeDensityMapComparison: null,
          candidateMapPois: [],
          selectedMapPoi: null
        });
        setErrorMessage(portfolioRouteQualityMessage(error));
        return false;
      }
      if (isPlanProposalExpiredError(error)) {
        plannerStore.setState({
          activeDensityMapComparison: null,
          candidateMapPois: [],
          selectedMapPoi: null
        });
        setErrorMessage(error.message);
        return false;
      }
      if (payload.selectedAgentChoice && isRefreshableAgentChoiceError(error)) {
        const refreshed = await refreshAgentChoiceSession(selectedChoiceSessionId);
        const proposalCarrierChanged = isPlanProposalRequestScopeInvalidError(error);
        setErrorMessage(
          proposalCarrierChanged
            ? refreshed
              ? "方案确认入口已更新，已加载当前会话的最新方案，请重新点击确认。"
              : "方案确认入口已更新，但最新方案加载失败，请刷新会话后重试。"
            : refreshed
              ? "该候选或行程版本已变化，已恢复当前会话的最新候选，请重新选择。"
              : "该候选或行程版本已变化；最新候选加载失败，请刷新会话后重试。"
        );
        return false;
      }
      setErrorMessage(friendlyAgentError(error instanceof Error ? error.message : "Agent 生成失败，请稍后重试"));
      return false;
    } finally {
      if (agentRequestSeqRef.current === requestSeq) {
        agentAbortControllerRef.current = null;
        setIsGenerating(false);
        generationLockRef.current = false;
        setPendingPlanningStep(null);
        setAgentRunStartedAtMs(null);
        setAgentRunElapsedMs(0);
      }
    }
    return true;
  }

  function handleStopAgentRun() {
    const sessionId = plannerStore.getSnapshot().agentSession?.sessionId;
    if (sessionId) {
      void apiClient.cancelAgentRun(sessionId).catch(() => undefined);
    }
    agentAbortControllerRef.current?.abort();
    agentAbortControllerRef.current = null;
    agentRequestSeqRef.current += 1;
    reasoningRecoverySeqRef.current += 1;
    generationLockRef.current = false;
    setIsGenerating(false);
    setEditingTurnSubmittingId(null);
    setPendingAgentMessage("");
    setPendingPlanningStep(null);
    setLiveReasoningStatuses((current) =>
      closeReasoningStatuses(current, "cancelled", sessionId ?? "session", agentRunElapsedMs)
    );
    setAgentRunStartedAtMs(null);
    setAgentRunElapsedMs(0);
    setErrorMessage("已停止等待本轮 Agent 响应；如果后端已进入安全写入阶段，将在该阶段完成后终态化。");
  }

  async function handleRetryCandidateHints() {
    await handleAgentMessageSubmit({
      cityHint: plannerStore.getSnapshot().selectedCity,
      textItems: ["请让 Agent 自动补充候选语义提示后继续规划。"],
      socialLinks: [],
      files: [],
      sourceKind: "screenshot",
      saveOriginalImages: false,
      retryCandidateHints: true
    });
  }

  async function handleManualCandidateHints() {
    const raw = window.prompt("输入候选名称，用中文逗号或换行分隔。");
    const candidateHints = (raw ?? "")
      .split(/[，,\n]/)
      .map((item) => item.trim())
      .filter(Boolean);
    if (!candidateHints.length) {
      return;
    }
    await handleAgentMessageSubmit({
      cityHint: plannerStore.getSnapshot().selectedCity,
      textItems: [`我来输入候选：${candidateHints.join("、")}`],
      socialLinks: [],
      files: [],
      sourceKind: "screenshot",
      saveOriginalImages: false,
      manualCandidateHints: [
        {
          intentType: "campus_visit",
          candidateHints,
          hintPolicy: "user_explicit_hint"
        },
        {
          intentType: "night_view",
          candidateHints,
          hintPolicy: "user_explicit_hint"
        }
      ]
    });
  }

  async function handleClarificationOptionSelect(option: ClarificationOption, turn: ConversationTurn) {
    if (isOutdatedComparisonChoice(option, plannerStore.getSnapshot().comparisonPreview)) {
      setErrorMessage("此方案操作已由后续规划请求取代；旧方案仍可在行程对比中只读查看。");
      return;
    }
    if (option.action === "open_map_selection" || option.action === "open_density_map") {
      const snapshot = plannerStore.getSnapshot();
      const candidateRecordId = option.candidateRecordId ?? option.selectionGroupId ?? "";
      if (
        option.action === "open_density_map" &&
        (!snapshot.agentSession?.sessionId ||
          !candidateRecordId ||
          !option.briefId ||
          !option.poolId ||
          !option.dayNumber ||
          !option.planningSlotId)
      ) {
        return;
      }
      const densityComparison =
        option.action === "open_density_map" &&
        snapshot.agentSession?.sessionId &&
        candidateRecordId &&
        option.dayNumber &&
        option.planningSlotId
          ? {
              sessionId: snapshot.agentSession.sessionId,
              sourceAssistantTurnId: turn.id,
              candidateRecordId,
              briefId: option.briefId ?? "",
              poolId: option.poolId ?? "",
              dayNumber: option.dayNumber,
              planningSlotId: option.planningSlotId,
              timeWindow: option.timeWindow ?? "待排时段",
              displayNeed: option.displayNeed ?? "候选地点",
              anchors: option.comparisonAnchors ?? [],
              candidateChoices: normalizeStructuredOptions(turn.choiceOptions)
                .filter(
                  (candidate) =>
                    candidate.action === "resume_density_candidate" &&
                    candidate.candidateRecordId === candidateRecordId &&
                    candidate.briefId === option.briefId &&
                    candidate.poolId === option.poolId &&
                    candidate.planningSlotId === option.planningSlotId &&
                    candidate.dayNumber === option.dayNumber &&
                    Boolean(candidate.id && candidate.amapId)
                )
                .map((candidate) => ({
                  amapId: String(candidate.amapId),
                  sourceAssistantTurnId: turn.id,
                  choiceId: String(candidate.id),
                  label: candidate.label
                }))
            }
          : null;
      plannerStore.setState({
        activeDensityMapComparison: densityComparison,
        selectedDayNumber: option.dayNumber ?? plannerStore.getSnapshot().selectedDayNumber,
        comparisonPreview: snapshot.comparisonPreview.focusedProposalId
          ? beginPendingSlotComparison(snapshot.comparisonPreview, snapshot.comparisonPreview.focusedProposalId)
          : snapshot.comparisonPreview
      });
      setActiveWorkspacePanel("map");
      return;
    }
    const choiceId = option.id;
    if (!choiceId) {
      setErrorMessage("该选项缺少服务端选择身份，无法提交；请刷新后重试。");
      return;
    }
    const choiceKey = `${turn.id}:${choiceId}`;
    setExecutingAgentChoiceKey(choiceKey);
    await handleAgentMessageSubmit({
      cityHint: plannerStore.getSnapshot().selectedCity,
      textItems: [option.label],
      socialLinks: [],
      files: [],
      sourceKind: "screenshot",
      saveOriginalImages: false,
      selectedAgentChoice: { sourceAssistantTurnId: turn.id, choiceId }
    });
    setExecutingAgentChoiceKey((current) => (current === choiceKey ? null : current));
  }

  async function handleClarificationBatchSubmit(
    option: ClarificationOption,
    selections: ClarificationBatchSelection[],
    turn: ConversationTurn
  ) {
    const choiceId = option.id ?? "";
    if (!choiceId || option.action !== "submit_clarification_batch") {
      return false;
    }
    const choiceKey = `${turn.id}:${choiceId}`;
    setExecutingAgentChoiceKey(choiceKey);
    const succeeded = await handleAgentMessageSubmit({
      cityHint: plannerStore.getSnapshot().selectedCity,
      textItems: [option.label || "提交关键决策"],
      socialLinks: [],
      files: [],
      sourceKind: "screenshot",
      saveOriginalImages: false,
      selectedAgentChoice: {
        sourceAssistantTurnId: turn.id,
        choiceId,
        batchSelections: selections
      }
    });
    setExecutingAgentChoiceKey((current) => (current === choiceKey ? null : current));
    return succeeded;
  }

  async function handleDensityMapCandidateConfirm(choice: {
    amapId: string;
    sourceAssistantTurnId: string;
    choiceId: string;
  }) {
    const choiceKey = `${choice.sourceAssistantTurnId}:${choice.choiceId}`;
    setExecutingAgentChoiceKey(choiceKey);
    setActiveWorkspacePanel("agent");
    await handleAgentMessageSubmit({
      cityHint: plannerStore.getSnapshot().selectedCity,
      textItems: ["确认地图中的真实候选"],
      socialLinks: [],
      files: [],
      sourceKind: "screenshot",
      saveOriginalImages: false,
      selectedAgentChoice: {
        sourceAssistantTurnId: choice.sourceAssistantTurnId,
        choiceId: choice.choiceId
      }
    });
    setExecutingAgentChoiceKey((current) => (current === choiceKey ? null : current));
  }

  async function refreshAgentChoiceSession(sessionId?: string) {
    if (!sessionId || plannerStore.getSnapshot().agentSession?.sessionId !== sessionId) {
      return false;
    }
    try {
      const refreshed = await apiClient.getAgentSession(sessionId);
      if (plannerStore.getSnapshot().agentSession?.sessionId === sessionId) {
        applyAgentSession(refreshed);
        return true;
      }
    } catch {
      // Preserve the original server error if the readback itself fails.
    }
    return false;
  }

  async function handleClarificationCustomOptionSubmit(
    option: ClarificationOption,
    value: string,
    turn: ConversationTurn
  ) {
    const trimmed = value.trim();
    if (!trimmed) {
      return;
    }
    if (isOutdatedComparisonChoice(option, plannerStore.getSnapshot().comparisonPreview)) {
      setErrorMessage("此方案操作已由后续规划请求取代；旧方案仍可在行程对比中只读查看。");
      return;
    }
    const choiceId = option.id;
    if (!choiceId) {
      setErrorMessage("该选项缺少服务端选择身份，无法提交；请刷新后重试。");
      return;
    }
    const choiceKey = `${turn.id}:${choiceId}`;
    setExecutingAgentChoiceKey(choiceKey);
    await handleAgentMessageSubmit({
      cityHint: plannerStore.getSnapshot().selectedCity,
      textItems: [trimmed],
      socialLinks: [],
      files: [],
      sourceKind: "screenshot",
      saveOriginalImages: false,
      selectedAgentChoice: { sourceAssistantTurnId: turn.id, choiceId, manualValue: trimmed }
    });
    setExecutingAgentChoiceKey((current) => (current === choiceKey ? null : current));
  }

  function applyStructuredChoiceLifecycle(trace: ConversationTurn["structuredChoiceTrace"]) {
    const sourceTurnId = String(trace?.sourceAssistantTurnId ?? "").trim();
    const choiceId = String(trace?.resolvedChoiceId ?? "").trim();
    const executionStatus = trace?.executionStatus;
    if (!sourceTurnId || !choiceId || !executionStatus) {
      return;
    }
    const lifecycle: AgentChoiceOption["lifecycle"] = executionStatus === "succeeded" ? "consumed" : executionStatus;
    setAgentChoiceLifecycleByKey((current) => ({
      ...current,
      [`${sourceTurnId}:${choiceId}`]: lifecycle
    }));
  }

  function handleComparisonPlanFocus(proposalId: string, requestedDayNumber?: number) {
    const current = plannerStore.getSnapshot();
    const selectedPlan = current.comparisonPreview.plans.find((plan) => plan.proposalId === proposalId);
    if (!selectedPlan) return;
    const selectedDayNumber =
      typeof requestedDayNumber === "number" && selectedPlan.days.some((day) => day.dayNumber === requestedDayNumber)
        ? requestedDayNumber
        : (selectedPlan.days[0]?.dayNumber ?? current.selectedDayNumber);
    plannerStore.setState({
      comparisonPreview: focusComparisonPlan(current.comparisonPreview, proposalId),
      selectedDayNumber,
      selectedSegmentId: null,
      selectedRouteOptionId: null,
      previewRouteOptionId: null,
      selectedMapPoi: null
    });
  }

  function handleComparisonPlanDetails() {
    setActiveItineraryTab("overview");
  }

  async function handleComparisonPlanRepair(plan: ComparisonPlanProjection) {
    const repairChoiceId = plan.repairChoiceId?.trim();
    if (!repairChoiceId || !plan.sourceAssistantTurnId.trim()) {
      setComparisonError("服务端没有为此方向签发可执行的补全操作；当前仅保留只读缺口说明。");
      setActiveItineraryTab("comparison");
      return;
    }
    const choiceKey = `${plan.sourceAssistantTurnId}:${repairChoiceId}`;
    if (agentChoiceInFlightRef.current.has(choiceKey)) return;
    handleComparisonPlanFocus(plan.proposalId);
    agentChoiceInFlightRef.current.add(choiceKey);
    setExecutingAgentChoiceKey(choiceKey);
    try {
      await handleAgentMessageSubmit({
        cityHint: plannerStore.getSnapshot().selectedCity,
        textItems: [`补全「${plan.title || "该方向"}」`],
        socialLinks: [],
        files: [],
        sourceKind: "screenshot",
        saveOriginalImages: false,
        selectedAgentChoice: {
          sourceAssistantTurnId: plan.sourceAssistantTurnId,
          choiceId: repairChoiceId
        }
      });
    } finally {
      agentChoiceInFlightRef.current.delete(choiceKey);
      setExecutingAgentChoiceKey((current) => (current === choiceKey ? null : current));
    }
  }

  async function handleComparisonPlanAdopt(plan: ComparisonPlanProjection) {
    const isSimpleDirection = plan.workflowMode === "simple_direction_v1";
    if (!isCurrentComparisonScope(plannerStore.getSnapshot().comparisonPreview, plan)) {
      setComparisonError("该方案来自历史规划，仅供对比；请操作最新一轮方案。");
      setActiveItineraryTab("comparison");
      return;
    }
    if (plan.nextAction === "complete_pending_slots" || plan.nextAction === "continue_grounding_hard_slots") {
      const current = plannerStore.getSnapshot();
      plannerStore.setState({
        comparisonPreview: beginPendingSlotComparison(current.comparisonPreview, plan.proposalId)
      });
      setActiveItineraryTab("overview");
      return;
    }
    if (!isSimpleDirection && plan.nextAction === "continue_editing") {
      const current = plannerStore.getSnapshot();
      const focused = focusComparisonPlan(current.comparisonPreview, plan.proposalId);
      plannerStore.setState({
        comparisonPreview: {
          ...focused,
          mapMode: "itinerary_edit",
          isMapReadOnly: false
        }
      });
      setActiveItineraryTab("overview");
      return;
    }
    if (plan.nextAction === "none") {
      return;
    }
    const choiceKey = `${plan.sourceAssistantTurnId}:${plan.choiceId}`;
    if (agentChoiceInFlightRef.current.has(choiceKey)) {
      return;
    }
    agentChoiceInFlightRef.current.add(choiceKey);
    setExecutingAgentChoiceKey(choiceKey);
    const submittedChoiceLabel = isSimpleDirection
      ? plan.nextActionLabel?.trim() && plan.nextActionLabel.trim() !== "确认编辑"
        ? plan.nextActionLabel
        : `确认编辑「${plan.title || "该行程方向"}」`
      : plan.nextActionLabel || "核验路线并采用";
    try {
      const succeeded = await handleAgentMessageSubmit({
        cityHint: plannerStore.getSnapshot().selectedCity,
        textItems: [submittedChoiceLabel],
        socialLinks: [],
        files: [],
        sourceKind: "screenshot",
        saveOriginalImages: false,
        selectedAgentChoice: {
          sourceAssistantTurnId: plan.sourceAssistantTurnId,
          choiceId: plan.choiceId
        }
      });
      if (succeeded && isSimpleDirection) {
        const current = plannerStore.getSnapshot();
        if (!isCurrentComparisonScope(current.comparisonPreview, plan)) {
          setComparisonError("本次确认对应的方向已被新一轮规划取代；服务端结果不会切换到编辑态，请操作最新方案。");
          setActiveItineraryTab("comparison");
          return;
        }
        const confirmationTurn = [...current.conversationTurns]
          .reverse()
          .find(
            (turn) =>
              turn.structuredChoiceTrace?.sourceAssistantTurnId === plan.sourceAssistantTurnId &&
              turn.structuredChoiceTrace?.resolvedChoiceId === plan.choiceId &&
              turn.structuredChoiceTrace?.executionStatus === "succeeded"
          );
        const tracedVersionId = String(
          confirmationTurn?.structuredChoiceTrace?.resultVersionId ?? confirmationTurn?.itineraryVersionId ?? ""
        ).trim();
        const versionId = current.activeVersionId;
        if (!versionId || !tracedVersionId || tracedVersionId !== versionId || !current.itineraryPlan) {
          setComparisonError("服务端未返回与本次确认一致的可编辑行程版本；当前仍停留在行程对比，请重试确认。");
          setActiveItineraryTab("comparison");
          return;
        }
        const editingPreview = markComparisonPlanAdopted(current.comparisonPreview, plan.proposalId, versionId);
        if (editingPreview.adoptedProposalId !== plan.proposalId) {
          setComparisonError("服务端已返回行程版本，但对应方向快照缺失；当前仍停留在行程对比。");
          setActiveItineraryTab("comparison");
          return;
        }
        plannerStore.setState({ comparisonPreview: editingPreview });
        setComparisonError("");
        setActiveItineraryTab("overview");
        setActiveWorkspacePanel("timeline");
      }
    } finally {
      agentChoiceInFlightRef.current.delete(choiceKey);
      setExecutingAgentChoiceKey((current) => (current === choiceKey ? null : current));
    }
  }

  async function handleComparisonThemeCompletion(plan: ComparisonPlanProjection) {
    if (!isCurrentComparisonScope(plannerStore.getSnapshot().comparisonPreview, plan)) {
      setComparisonError("该方案来自历史规划，仅供对比；请操作最新一轮方案。");
      setActiveItineraryTab("comparison");
      return;
    }
    const action = plan.completionAction;
    if (!action?.choiceId) return;
    const choiceKey = `${plan.sourceAssistantTurnId}:${action.choiceId}`;
    setExecutingAgentChoiceKey(choiceKey);
    try {
      await handleAgentMessageSubmit({
        cityHint: plannerStore.getSnapshot().selectedCity,
        textItems: [action.label],
        socialLinks: [],
        files: [],
        sourceKind: "screenshot",
        saveOriginalImages: false,
        selectedAgentChoice: {
          sourceAssistantTurnId: plan.sourceAssistantTurnId,
          choiceId: action.choiceId
        }
      });
    } finally {
      setExecutingAgentChoiceKey((current) => (current === choiceKey ? null : current));
    }
  }

  async function handleEditConversationTurn(turnId: string) {
    const content = editingTurnContent.trim();
    const snapshot = plannerStore.getSnapshot();
    if (!content || !snapshot.agentSession) {
      setErrorMessage("请输入要更新的对话消息。");
      return;
    }
    setIsGenerating(true);
    setEditingTurnSubmittingId(turnId);
    setPendingAgentMessage(content);
    setPendingPlanningStep(null);
    setLiveReasoningStatuses([]);
    setAgentRunStartedAtMs(Date.now());
    setAgentRunElapsedMs(0);
    const requestSeq = ++agentRequestSeqRef.current;
    const abortController = new AbortController();
    agentAbortControllerRef.current = abortController;
    setErrorMessage("");
    try {
      const response = await apiClient.editAgentMessage(
        snapshot.agentSession.sessionId,
        turnId,
        {
          content,
          regenerate: true,
          agentModel: selectedAgentModel,
          context: buildAgentContext(snapshot, snapshot.preferenceCard ?? defaultPreferenceCard(), content, {
            activeView: activeItineraryTab === "comparison" ? "comparison" : "overview"
          })
        },
        {
          signal: abortController.signal
        }
      );
      if (!isCurrentAgentRequest(agentRequestSeqRef, requestSeq, snapshot.agentSession.sessionId)) {
        return;
      }
      const existingTurns = replaceEditedConversationTurn(
        plannerStore.getSnapshot().conversationTurns,
        turnId,
        response.editedTurn,
        response.supersededTurnIds
      );
      const nextTurns = response.assistantTurn
        ? upsertConversationTurn(existingTurns, response.assistantTurn)
        : existingTurns;
      const nextItinerary = response.itinerary ?? snapshot.itineraryPlan;
      const firstDay = firstPopulatedDay(nextItinerary);
      plannerStore.setState({
        agentSession: {
          ...snapshot.agentSession,
          activeVersionId: response.version?.id ?? response.restoredVersionId,
          turns: nextTurns,
          itinerary: response.itinerary,
          pendingPoiCandidates: response.pendingPoiCandidates
        },
        conversationTurns: nextTurns,
        supersededTurnIds: response.supersededTurnIds,
        activeVersionId: response.version?.id ?? response.restoredVersionId,
        itineraryPlan: response.itinerary ?? snapshot.itineraryPlan,
        pendingPoiCandidates: response.pendingPoiCandidates,
        selectedDayNumber: firstDay?.dayNumber ?? snapshot.selectedDayNumber,
        selectedSegmentId: firstDay?.segments[0]?.id ?? snapshot.selectedSegmentId,
        selectedRouteOptionId:
          selectedRouteForSegment(nextItinerary, firstDay?.segments[0]?.id ?? snapshot.selectedSegmentId)?.id ?? null,
        previewRouteOptionId: null,
        routeWarnings: response.itinerary?.routeWarnings ?? snapshot.routeWarnings,
        lastPlanningRun: response.planningRun ?? snapshot.lastPlanningRun
      });
      setPlanningProcess(null);
      setEditingTurnId(null);
      setEditingTurnContent("");
      setPendingAgentMessage("");
      setPendingPlanningStep(null);
      setAgentRunStartedAtMs(null);
      setAgentRunElapsedMs(0);
      if (response.assistantTurn?.status === "failed") {
        setErrorMessage(friendlyAgentError(response.warnings[0] ?? "Agent 输出未通过校验，行程已回滚到历史状态。"));
      }
    } catch (error) {
      if (
        !isCurrentAgentRequest(agentRequestSeqRef, requestSeq, snapshot.agentSession.sessionId) ||
        abortController.signal.aborted
      ) {
        return;
      }
      setErrorMessage(friendlyAgentError(error instanceof Error ? error.message : "历史消息编辑失败，请稍后重试"));
    } finally {
      if (agentAbortControllerRef.current === abortController) {
        agentAbortControllerRef.current = null;
      }
      if (agentRequestSeqRef.current === requestSeq) {
        setIsGenerating(false);
        setEditingTurnSubmittingId(null);
        setPendingAgentMessage("");
        setPendingPlanningStep(null);
        setAgentRunStartedAtMs(null);
        setAgentRunElapsedMs(0);
      }
    }
  }

  async function handleResumeAgentTurn(turnId: string) {
    const snapshot = plannerStore.getSnapshot();
    if (!snapshot.agentSession) {
      setErrorMessage("当前没有可续跑的 Agent 会话。");
      return;
    }
    setIsGenerating(true);
    const requestSeq = ++agentRequestSeqRef.current;
    const abortController = new AbortController();
    agentAbortControllerRef.current = abortController;
    setErrorMessage("");
    try {
      const response = await apiClient.resumeAgentTurn(snapshot.agentSession.sessionId, turnId, {
        signal: abortController.signal
      });
      if (!isCurrentAgentRequest(agentRequestSeqRef, requestSeq, snapshot.agentSession.sessionId)) {
        return;
      }
      const nextTurns = upsertConversationTurn(
        upsertConversationTurn(plannerStore.getSnapshot().conversationTurns, response.userTurn),
        response.assistantTurn
      );
      const nextItinerary = response.itinerary ?? snapshot.itineraryPlan;
      const firstDay = firstPopulatedDay(nextItinerary);
      plannerStore.setState({
        agentSession: {
          ...snapshot.agentSession,
          activeVersionId: response.version?.id ?? snapshot.agentSession.activeVersionId,
          turns: nextTurns,
          itinerary: nextItinerary,
          pendingPoiCandidates: response.pendingPoiCandidates
        },
        conversationTurns: nextTurns,
        activeVersionId: response.version?.id ?? snapshot.activeVersionId,
        itineraryPlan: nextItinerary,
        pendingPoiCandidates: response.pendingPoiCandidates,
        selectedDayNumber: firstDay?.dayNumber ?? snapshot.selectedDayNumber,
        selectedSegmentId: firstDay?.segments[0]?.id ?? snapshot.selectedSegmentId,
        selectedRouteOptionId:
          selectedRouteForSegment(nextItinerary, firstDay?.segments[0]?.id ?? snapshot.selectedSegmentId)?.id ?? null,
        previewRouteOptionId: null,
        routeWarnings: response.itinerary?.routeWarnings ?? snapshot.routeWarnings,
        lastPlanningRun: response.planningRun ?? snapshot.lastPlanningRun
      });
      if (response.assistantTurn.status === "failed") {
        setErrorMessage(friendlyAgentError(response.warnings[0] ?? "续跑失败，行程未被覆盖。"));
      }
    } catch (error) {
      if (
        !isCurrentAgentRequest(agentRequestSeqRef, requestSeq, snapshot.agentSession.sessionId) ||
        abortController.signal.aborted
      ) {
        return;
      }
      setErrorMessage(friendlyAgentError(error instanceof Error ? error.message : "续跑失败，请稍后重试"));
    } finally {
      if (agentAbortControllerRef.current === abortController) {
        agentAbortControllerRef.current = null;
      }
      if (agentRequestSeqRef.current === requestSeq) {
        setIsGenerating(false);
      }
    }
  }

  async function handleCopyDebug() {
    try {
      let capturedSession = plannerSnapshot.agentSession;
      let capturedTurns = plannerSnapshot.conversationTurns;
      let capturedVersionId = plannerSnapshot.activeVersionId;
      let plan = plannerSnapshot.itineraryPlan;
      let sessionCaptureSource: "server_refresh" | "local_fallback" = "local_fallback";
      let sessionCaptureError: string | null = null;
      let serverDebugBundle: unknown = null;
      const sessionId = plannerSnapshot.agentSession?.sessionId;
      if (sessionId) {
        try {
          const refreshed = await apiClient.getAgentSession(sessionId);
          capturedSession = refreshed;
          capturedTurns = refreshed.turns;
          capturedVersionId = refreshed.activeVersionId ?? null;
          plan = refreshed.itinerary;
          sessionCaptureSource = "server_refresh";
        } catch (error) {
          sessionCaptureError = error instanceof Error ? error.message : "session_refresh_failed";
        }
      }
      if (sessionId) {
        try {
          serverDebugBundle = await apiClient.exportAgentDebugBundle(sessionId);
          sessionCaptureSource = "server_refresh";
        } catch (error) {
          sessionCaptureError = error instanceof Error ? error.message : "server_debug_bundle_unavailable";
        }
      }
      const captured = {
        capturedAt: new Date().toISOString(),
        session: capturedSession,
        activeVersionId: capturedVersionId,
        turns: capturedTurns,
        planningProcess,
        pendingPlanningStep,
        itinerary: plan,
        timelineText:
          plannerSnapshot.timelineCopyText ||
          (plan ? formatTimelineForCopy(plan, plan.title, createEditableDays(plan)) : ""),
        sessionCaptureSource,
        sessionCaptureError,
        comparisonState: plannerSnapshot.comparisonPreview,
        mapInteraction: mapInteractionDebugRef.current,
        serverDebugBundle,
        visibleError: comparisonError || preferenceError || null,
        modelDisplayName: selectedAgentModel
      };
      const debugObject = serverDebugBundle as {
        sections?: { META?: { planningRootId?: unknown } };
      } | null;
      const rootId = String(debugObject?.sections?.META?.planningRootId ?? "no-root");
      const filename = `trip-agent-debug-${safeFilename(sessionId ?? "session")}-${safeFilename(rootId)}-${Date.now()}.json`;
      const delivery = await deliverTripDebugBundle({
        text: buildTripTestBundle(captured),
        json: buildTripDebugBundleJson(captured),
        filename,
        copy: copyTextToClipboard,
        download: (name, value) => downloadTextFile(name, `${value}\n`, "application/json;charset=utf-8")
      });
      setDebugCopyState(delivery);
      window.setTimeout(() => setDebugCopyState("idle"), 1600);
    } catch {
      setDebugCopyState("failed");
      window.setTimeout(() => setDebugCopyState("idle"), 2200);
    }
  }

  async function handleExportPlanningTrace(sessionId: string, assistantTurnId: string, planningRunId: string) {
    const trace = await apiClient.exportPlanningTrace(sessionId, assistantTurnId, planningRunId);
    const text = `${JSON.stringify(trace, null, 2)}\n`;
    // Bound clipboard work by UTF-8 size; the downloaded JSON is never truncated.
    if (new Blob([text]).size <= 1024 * 1024) {
      try {
        await copyTextToClipboard(text);
        return "copied" as const;
      } catch {
        // Browsers may deny clipboard access even when the content fits.
      }
    }
    downloadTextFile(
      `trip-planning-trace-${safeFilename(planningRunId)}.json`,
      text,
      "application/json;charset=utf-8"
    );
    return "downloaded" as const;
  }
  async function handleCopyConversationTurn(turn: ConversationTurn) {
    if (messageCopyResetTimerRef.current !== null) {
      window.clearTimeout(messageCopyResetTimerRef.current);
    }
    try {
      await copyTextToClipboard(turn.content);
      setMessageCopyState({ turnId: turn.id, status: "copied" });
    } catch {
      setMessageCopyState({ turnId: turn.id, status: "failed" });
    }
    messageCopyResetTimerRef.current = window.setTimeout(() => {
      setMessageCopyState((current) => (current?.turnId === turn.id ? null : current));
      messageCopyResetTimerRef.current = null;
    }, 1800);
  }

  async function handleSaveCurrentVersion() {
    const plan = plannerSnapshot.itineraryPlan;
    const versionId = plannerSnapshot.activeVersionId;
    if (!plan || !versionId) {
      setSaveVersionState("failed");
      window.setTimeout(() => setSaveVersionState("idle"), 2200);
      return;
    }
    setSaveVersionState("saving");
    try {
      const saved = await apiClient.saveItineraryVersion(plan.id, versionId);
      setSavedVersionIds((current) => new Set([...current, saved.versionId]));
      setSaveVersionState("saved");
      window.setTimeout(() => setSaveVersionState("idle"), 1800);
    } catch {
      setSaveVersionState("failed");
      window.setTimeout(() => setSaveVersionState("idle"), 2400);
    }
  }

  async function handleExportMarkdown() {
    const plan = plannerSnapshot.itineraryPlan;
    if (!plan) {
      return;
    }
    setExportState("exporting");
    try {
      const text = await apiClient.exportItineraryMarkdown(plan.id);
      downloadTextFile(`${safeFilename(plan.title || "trip-itinerary")}.md`, text, "text/markdown;charset=utf-8");
      setExportState("exported");
      window.setTimeout(() => setExportState("idle"), 1600);
    } catch {
      setExportState("failed");
      window.setTimeout(() => setExportState("idle"), 2400);
    }
  }

  async function handleExportJson() {
    const plan = plannerSnapshot.itineraryPlan;
    if (!plan) {
      return;
    }
    setExportState("exporting");
    try {
      const payload = await apiClient.exportItineraryJson(plan.id);
      downloadTextFile(
        `${safeFilename(plan.title || "trip-itinerary")}.json`,
        JSON.stringify(payload, null, 2),
        "application/json;charset=utf-8"
      );
      setExportState("exported");
      window.setTimeout(() => setExportState("idle"), 1600);
    } catch {
      setExportState("failed");
      window.setTimeout(() => setExportState("idle"), 2400);
    }
  }

  async function handleSavePreferenceSummary(memoryText: string, autoUpdateEnabled: boolean) {
    const trimmed = memoryText.trim();
    if (!trimmed) {
      throw new Error("偏好卡片不能为空。");
    }
    const sessionId = plannerStore.getSnapshot().agentSession?.sessionId ?? null;
    const preferenceMemory = await apiClient.updatePreferenceMemory({ memoryText, autoUpdateEnabled }, sessionId);
    plannerStore.setState({
      preferenceMemory,
      preferenceCard: {
        ...(plannerStore.getSnapshot().preferenceCard ?? defaultPreferenceCard()),
        summaryText: preferenceMemory.memoryText
      }
    });
    return preferenceMemory;
  }

  async function handleRestorePreferenceMemory() {
    const sessionId = plannerStore.getSnapshot().agentSession?.sessionId ?? null;
    const preferenceMemory = await apiClient.restorePreferenceMemory(sessionId);
    plannerStore.setState({
      preferenceMemory,
      preferenceCard: {
        ...(plannerStore.getSnapshot().preferenceCard ?? defaultPreferenceCard()),
        summaryText: preferenceMemory.memoryText
      }
    });
    return preferenceMemory;
  }

  async function handleApplyLocalReplanSuggestion(suggestion: LocalReplanSuggestion) {
    const snapshot = plannerStore.getSnapshot();
    if (!snapshot.itineraryPlan) {
      setErrorMessage("当前没有可应用局部优化的行程。");
      return;
    }
    if (!suggestion.operations.length) {
      setErrorMessage("该建议暂无可自动应用的操作，请先手动调整行程。");
      return;
    }
    setIsGenerating(true);
    setApplyingSuggestionId(suggestion.id);
    setErrorMessage("");
    try {
      const baseVersionId = snapshot.activeVersionId;
      const result = await apiClient.patchItinerary(snapshot.itineraryPlan.id, {
        sourceType: "local_replan",
        baseVersionId: baseVersionId ?? null,
        preferenceSummary: effectivePreferenceMemoryText(
          snapshot.preferenceMemory?.memoryText ?? snapshot.preferenceCard?.summaryText ?? ""
        ),
        planningContext: buildPlanningContext(snapshot),
        operations: suggestion.operations as ItineraryPatchOperation[]
      });
      if (!isCurrentVersionedWrite(baseVersionId)) {
        return;
      }
      const movedSegmentId = findFirstOperationSegmentId(suggestion.operations);
      const selectedDay =
        result.itinerary.days.find((day) => day.segments.some((segment) => segment.id === movedSegmentId)) ??
        firstPopulatedDay(result.itinerary);
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
        selectedDayNumber: selectedDay?.dayNumber ?? snapshot.selectedDayNumber,
        selectedSegmentId: movedSegmentId ?? selectedDay?.segments[0]?.id ?? snapshot.selectedSegmentId,
        selectedRouteOptionId:
          selectedRouteForSegment(
            result.itinerary,
            movedSegmentId ?? selectedDay?.segments[0]?.id ?? snapshot.selectedSegmentId
          )?.id ?? null,
        previewRouteOptionId: null,
        routeWarnings: result.itinerary.routeWarnings ?? [],
        lastPlanningRun: result.planningRun ?? snapshot.lastPlanningRun
      });
      setPlanningProcess(null);
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "局部优化应用失败，请稍后重试。");
    } finally {
      setApplyingSuggestionId(null);
      setIsGenerating(false);
    }
  }

  function handleSelectSegment(segmentId: string) {
    const nextSelectionRequestId = plannerSnapshot.timelineSelectionRequestId + 1;
    const selectionPlan = displayedPlan ?? plannerSnapshot.itineraryPlan;
    if (!segmentExistsInPlan(selectionPlan, segmentId)) {
      plannerStore.setState({
        selectedMapPoi: null,
        candidateMapPois: [],
        selectedSegmentId: null,
        timelineSelectionRequestId: nextSelectionRequestId,
        selectedRouteOptionId: null,
        previewRouteOptionId: null
      });
      return;
    }
    const selectedRoute = selectedRouteForSegment(selectionPlan, segmentId);
    const selectedDayNumber = dayNumberForAppSegment(selectionPlan, segmentId) ?? plannerSnapshot.selectedDayNumber;
    plannerStore.setState({
      selectedSegmentId: segmentId,
      timelineSelectionRequestId: nextSelectionRequestId,
      selectedDayNumber,
      selectedRouteOptionId: selectedRoute?.id ?? null,
      previewRouteOptionId: null,
      selectedMapPoi: null
    });
  }

  function handlePendingTimelineSlotSelect(slot: PendingTimelineSlot) {
    const focusScopedChoice = () => {
      setAgentCollapsed(false);
      window.setTimeout(() => {
        const scopedButton = Array.from(
          agentChatRef.current?.querySelectorAll<HTMLButtonElement>("button[data-planning-slot-id]") ?? []
        ).find(
          (button) =>
            button.dataset.planningSlotId === slot.planningSlotId &&
            button.dataset.briefId === slot.briefId &&
            (button.dataset.poolId || undefined) === (slot.poolId ?? undefined) &&
            Number(button.dataset.dayNumber) === slot.dayNumber
        );
        if (scopedButton) {
          if (typeof scopedButton.scrollIntoView === "function") {
            scopedButton.scrollIntoView({ behavior: preferredScrollBehavior(), block: "center" });
          }
          scopedButton.focus();
          return;
        }
        const chat = agentChatRef.current;
        if (chat && typeof chat.scrollTo === "function") {
          chat.scrollTo({ top: chat.scrollHeight, behavior: preferredScrollBehavior() });
        }
      }, 0);
    };
    for (const turn of [...plannerStore.getSnapshot().conversationTurns].reverse()) {
      if (turn.role !== "assistant" || turn.status !== "active") {
        continue;
      }
      const scopedOptions = normalizeStructuredOptions(turn.choiceOptions).filter(
        (candidate) =>
          candidate.briefId === slot.briefId &&
          candidate.poolId === (slot.poolId ?? undefined) &&
          candidate.planningSlotId === slot.planningSlotId &&
          candidate.dayNumber === slot.dayNumber
      );
      const mapOption = scopedOptions.find((candidate) => candidate.action === "open_density_map");
      if (mapOption) {
        void handleClarificationOptionSelect(mapOption, turn);
        focusScopedChoice();
        return;
      }
      if (scopedOptions.length > 0) {
        focusScopedChoice();
        return;
      }
    }
    focusScopedChoice();
  }

  function handleAgentResizeStart(event: PointerEvent<HTMLButtonElement>) {
    event.currentTarget.setPointerCapture(event.pointerId);
    setIsResizingAgent(true);
  }

  function handleAgentResizeMove(event: PointerEvent<HTMLButtonElement>) {
    if (!isResizingAgent || agentCollapsed) {
      return;
    }
    const clientX = Number(event.clientX);
    if (!Number.isFinite(clientX)) {
      return;
    }
    const maxWidth = Math.min(
      480,
      window.innerWidth - timelineWidth - WORKSPACE_MAP_MIN_WIDTH - WORKSPACE_RESIZER_WIDTH
    );
    setAgentWidth(Math.min(Math.max(clientX, 360), maxWidth));
  }

  function handleAgentResizeEnd() {
    setIsResizingAgent(false);
  }

  function handleAgentResizeKeyDown(event: React.KeyboardEvent<HTMLButtonElement>) {
    if (event.key === "Home") {
      event.preventDefault();
      setAgentWidth(DEFAULT_AGENT_PANEL_WIDTH);
      return;
    }
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") {
      return;
    }
    event.preventDefault();
    setAgentWidth((width) => Math.min(480, Math.max(360, width + (event.key === "ArrowRight" ? 12 : -12))));
  }

  function handleTimelineResizeStart(event: PointerEvent<HTMLButtonElement>) {
    event.currentTarget.setPointerCapture(event.pointerId);
    setIsResizingTimeline(true);
  }

  function handleTimelineResizeMove(event: PointerEvent<HTMLButtonElement>) {
    if (!isResizingTimeline || timelineCollapsed) {
      return;
    }
    const clientX = Number(event.clientX);
    if (!Number.isFinite(clientX)) {
      return;
    }
    const maxWidth = Math.min(580, window.innerWidth - agentWidth - WORKSPACE_MAP_MIN_WIDTH - WORKSPACE_RESIZER_WIDTH);
    setTimelineWidth(Math.min(Math.max(window.innerWidth - clientX, 520), maxWidth));
  }

  function handleTimelineResizeEnd() {
    setIsResizingTimeline(false);
  }

  function handleTimelineResizeKeyDown(event: React.KeyboardEvent<HTMLButtonElement>) {
    if (event.key === "Home") {
      event.preventDefault();
      setTimelineWidth(DEFAULT_TIMELINE_PANEL_WIDTH);
      return;
    }
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") {
      return;
    }
    event.preventDefault();
    setTimelineWidth((width) => Math.min(580, Math.max(520, width + (event.key === "ArrowLeft" ? 12 : -12))));
  }

  function handlePreferenceTriggerKeyDown(event: React.KeyboardEvent<HTMLButtonElement>) {
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault();
    setPreferenceExpanded((expanded) => !expanded);
  }

  async function saveEditingDirectionBeforeComparison(): Promise<boolean> {
    if (directionSaveInFlightRef.current) {
      return directionSaveInFlightRef.current;
    }
    const operation = (async () => {
      const snapshot = plannerStore.getSnapshot();
      const preview = snapshot.comparisonPreview;
      const editingPlan = preview.plans.find(
        (plan) => plan.proposalId === preview.adoptedProposalId && plan.workflowMode === "simple_direction_v1"
      );
      if (preview.mapMode !== "itinerary_edit" || !editingPlan) return true;
      const sessionId = snapshot.agentSession?.sessionId;
      const baseVersionId = snapshot.activeVersionId;
      if (!sessionId || !baseVersionId) {
        setComparisonError("当前编辑行程缺少会话或版本标识，无法安全保存并返回行程对比。");
        return false;
      }
      setComparisonError("");
      try {
        const response = await apiClient.saveActiveDirection(sessionId, editingPlan.proposalId, {
          baseVersionId,
          planningSelectionRootTurnId: editingPlan.planningSelectionRootTurnId,
          rootPortfolioId: editingPlan.rootPortfolioId
        });
        const savedProjection = comparisonProjectionFromUnknown(response.comparisonProjection);
        if (
          response.saved !== true ||
          response.proposalId !== editingPlan.proposalId ||
          response.activeVersionId !== baseVersionId ||
          !savedProjection ||
          savedProjection.workflowMode !== "simple_direction_v1" ||
          savedProjection.proposalId !== editingPlan.proposalId ||
          savedProjection.planningSelectionRootTurnId !== editingPlan.planningSelectionRootTurnId ||
          savedProjection.rootPortfolioId !== editingPlan.rootPortfolioId
        ) {
          throw new Error("服务端保存结果与当前编辑方向不一致，已阻止切换。");
        }
        const refreshed = await apiClient.getAgentSession(sessionId);
        if (refreshed.sessionId !== sessionId || refreshed.activeVersionId !== response.activeVersionId) {
          throw new Error("保存后重新加载的活动版本不一致，已阻止切换。");
        }
        const live = plannerStore.getSnapshot();
        if (live.agentSession?.sessionId !== sessionId || live.activeVersionId !== baseVersionId) {
          throw new Error("自动保存期间行程版本已变化，已保留当前编辑视图，请重新返回行程对比。");
        }
        const reloadedPreview = comparisonPreviewFromTurns(refreshed.turns, preview);
        const hasReloadedProposal = reloadedPreview.plans.some(
          (plan) =>
            plan.proposalId === savedProjection.proposalId &&
            plan.planningSelectionRootTurnId === savedProjection.planningSelectionRootTurnId &&
            plan.rootPortfolioId === savedProjection.rootPortfolioId
        );
        if (!hasReloadedProposal) {
          throw new Error("保存后的方向快照未能从会话重新加载，已阻止切换。");
        }
        applyAgentSession(refreshed);
        const reloaded = plannerStore.getSnapshot();
        const updated = updateComparisonPlanSnapshot(reloaded.comparisonPreview, savedProjection);
        const readOnlyPreview = focusComparisonPlan(updated, savedProjection.proposalId);
        plannerStore.setState({
          comparisonPreview: {
            ...readOnlyPreview,
            mapMode: "plan_comparison_preview",
            isMapReadOnly: true
          }
        });
        setComparisonError("");
        return true;
      } catch (error) {
        setComparisonError(error instanceof Error ? error.message : "自动保存当前行程失败，已阻止切换。");
        return false;
      }
    })();
    directionSaveInFlightRef.current = operation;
    try {
      return await operation;
    } finally {
      if (directionSaveInFlightRef.current === operation) {
        directionSaveInFlightRef.current = null;
      }
    }
  }

  async function requestItineraryTabChange(nextTab: "overview" | "comparison" | "costs", focusTab = false) {
    if (nextTab === activeItineraryTab) {
      if (focusTab) document.getElementById(`itinerary-tab-${nextTab}`)?.focus();
      return;
    }
    if (nextTab === "comparison") {
      const snapshot = plannerStore.getSnapshot();
      const requiresDirectionSave = Boolean(
        snapshot.comparisonPreview.mapMode === "itinerary_edit" &&
        snapshot.comparisonPreview.plans.some(
          (plan) =>
            plan.proposalId === snapshot.comparisonPreview.adoptedProposalId &&
            plan.workflowMode === "simple_direction_v1"
        )
      );
      if (requiresDirectionSave && !(await saveEditingDirectionBeforeComparison())) {
        return;
      }
    }
    setActiveItineraryTab(nextTab);
    if (focusTab) document.getElementById(`itinerary-tab-${nextTab}`)?.focus();
  }

  function handleItineraryTabKeyDown(
    event: React.KeyboardEvent<HTMLButtonElement>,
    currentTab: "overview" | "comparison" | "costs"
  ) {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") {
      return;
    }
    event.preventDefault();
    const tabs: Array<typeof currentTab> = ["overview", "comparison", "costs"];
    const nextTab =
      tabs[(tabs.indexOf(currentTab) + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length];
    void requestItineraryTabChange(nextTab, true);
  }

  const showSemanticHintActions = errorMessage.includes("缺少候选语义提示");

  const focusedComparisonPlan =
    plannerSnapshot.comparisonPreview.plans.find(
      (plan) => plan.proposalId === plannerSnapshot.comparisonPreview.focusedProposalId
    ) ?? null;
  const comparisonMapActive = plannerSnapshot.comparisonPreview.mapMode !== "itinerary_edit";
  const previewPlan = useMemo(
    () => (focusedComparisonPlan ? comparisonProjectionToItinerary(focusedComparisonPlan) : null),
    [focusedComparisonPlan]
  );
  const comparisonMapModel = useMemo(
    () => comparisonPreviewMapModel(plannerSnapshot.comparisonPreview),
    [plannerSnapshot.comparisonPreview]
  );
  const displayedPlan =
    plannerSnapshot.comparisonPreview.mapMode === "plan_comparison_preview"
      ? comparisonMapModel.plan
      : comparisonMapActive && previewPlan
        ? previewPlan
        : plannerSnapshot.itineraryPlan;
  const currentClarificationTurnId = activeClarificationTurnId(plannerSnapshot.conversationTurns);
  const clarificationProgressTurnId = latestClarificationCheckpointTurnId(plannerSnapshot.conversationTurns);
  const visibleConversationTurns = useMemo(
    () => plannerSnapshot.conversationTurns.filter((turn) => turn.status !== "internal_capability"),
    [plannerSnapshot.conversationTurns]
  );
  const activeSpatialBoundaryPreview = useMemo(
    () =>
      [...plannerSnapshot.conversationTurns].reverse().find(isCurrentSpatialBoundaryPreviewTurn)
        ?.spatialBoundaryPreview ?? null,
    [plannerSnapshot.conversationTurns]
  );

  return (
    <main
      className={`app-shell workspace-panel-${activeWorkspacePanel} ${agentCollapsed ? "agent-collapsed" : ""} ${timelineCollapsed ? "timeline-collapsed" : ""}`}
      style={
        {
          "--agent-panel-width": `${agentCollapsed ? 54 : agentWidth}px`,
          "--timeline-panel-width": `${timelineCollapsed ? 54 : timelineWidth}px`
        } as CSSProperties
      }
    >
      <header className="top-command-bar" aria-label="Trip workspace status">
        <div className="brand-lockup">
          <div className="brand-mark" aria-hidden="true">
            AI
          </div>
          <div>
            <h1>
              去哪玩AI <span className="brand-description">行程规划师</span> <span className="brand-beta">Beta</span>
            </h1>
            <p>你的专属旅行规划智能体</p>
          </div>
        </div>
        <WorkspaceToolbar>
        <div className="workspace-meta">
          <label className="city-switcher">
            <span className="sr-only">当前城市</span>
            <select
              aria-label="当前城市"
              value={selectedCity}
              onChange={(event) => plannerStore.setState({ selectedCity: event.target.value })}
            >
              <option value="北京">北京市</option>
              <option value="上海">上海市</option>
              <option value="广州">广州市</option>
              <option value="深圳">深圳市</option>
            </select>
          </label>
          <label className="model-switcher">
            <span className="sr-only">选择 Agent 模型</span>
            <select
              aria-label="选择 Agent 模型"
              disabled={isGenerating}
              value={selectedAgentModel}
              onChange={(event) => setSelectedAgentModel(normalizeAgentModel(event.target.value))}
            >
              {availableAgentModels.map((model) => (
                <option key={model.id} value={model.id}>
                  {model.label}
                </option>
              ))}
            </select>
          </label>
          <span className="metric-chip">
            来源 <strong>{sourceCount}</strong>
          </span>
          <span className="metric-chip">
            最近查询 <strong>{lastQueried}</strong>
          </span>
          <div className="preference-note-anchor" ref={preferenceAnchorRef}>
            <button
              aria-expanded={preferenceExpanded}
              aria-label="旅行偏好卡片"
              className="preference-pin-trigger"
              onClick={() => setPreferenceExpanded((expanded) => !expanded)}
              onDoubleClick={() => setPreferenceExpanded((expanded) => !expanded)}
              onKeyDown={handlePreferenceTriggerKeyDown}
              title={preferenceExpanded ? "收起旅行偏好" : "展开旅行偏好"}
              type="button"
            >
              <Pin aria-hidden="true" size={16} strokeWidth={2.2} />
              <span>旅行偏好</span>
            </button>
            {preferenceExpanded ? (
              <aside aria-label="旅行偏好详情" className="floating-preference-note">
                <PreferenceSummaryCard
                  card={plannerSnapshot.preferenceCard}
                  memory={plannerSnapshot.preferenceMemory}
                  collapsed={false}
                  errorMessage={preferenceError}
                  onChange={(preferenceCard) => plannerStore.setState({ preferenceCard })}
                  onSave={handleSavePreferenceSummary}
                  onRestoreDefault={handleRestorePreferenceMemory}
                />
              </aside>
            ) : null}
          </div>
        </div>
        <div className="toolbar-actions">
          <label className="session-switcher">
            <span className="sr-only">选择对话</span>
            <select
              aria-label="选择对话"
              disabled={isGenerating}
              value={plannerSnapshot.agentSession?.sessionId ?? ""}
              onChange={(event) => void handleSwitchAgentSession(event.target.value)}
            >
              <option value="">无活动对话</option>
              {(plannerSnapshot.agentSessions ?? []).map((session) => (
                <option key={session.sessionId} value={session.sessionId}>
                  {session.title} · {session.turnCount} 轮
                </option>
              ))}
            </select>
          </label>
          <button type="button" disabled={isGenerating} onClick={() => void handleCreateAgentSession()}>
            新建对话
          </button>
          <button
            type="button"
            title="永久删除当前对话及其专属行程数据"
            disabled={isGenerating || !plannerSnapshot.agentSession}
            onClick={() => void handleDeleteAgentSession()}
          >
            删除对话及行程
          </button>
          <div className="toolbar-menu" ref={saveExportMenuRef}>
            <button
              aria-expanded={saveExportMenu === "save"}
              aria-haspopup="menu"
              disabled={isGenerating}
              onClick={() => setSaveExportMenu((current) => (current === "save" ? null : "save"))}
              type="button"
            >
              {saveVersionState === "failed"
                ? "保存失败"
                : saveVersionState === "saving"
                  ? "保存中"
                  : plannerSnapshot.activeVersionId && savedVersionIds.has(plannerSnapshot.activeVersionId)
                    ? "已保存"
                    : "保存"}
            </button>
            <button
              aria-expanded={saveExportMenu === "export"}
              aria-haspopup="menu"
              disabled={isGenerating}
              onClick={() => setSaveExportMenu((current) => (current === "export" ? null : "export"))}
              type="button"
            >
              {exportState === "failed" ? "导出失败" : exportState === "exported" ? "已导出" : "导出"}
            </button>
            {saveExportMenu === "save" ? (
              <div className="toolbar-menu-popover" role="menu">
                <button
                  disabled={
                    !plannerSnapshot.itineraryPlan || !plannerSnapshot.activeVersionId || saveVersionState === "saving"
                  }
                  onClick={() => {
                    setSaveExportMenu(null);
                    void handleSaveCurrentVersion();
                  }}
                  role="menuitem"
                  type="button"
                >
                  {saveVersionState === "failed"
                    ? "保存失败"
                    : saveVersionState === "saving"
                      ? "保存中"
                      : plannerSnapshot.activeVersionId && savedVersionIds.has(plannerSnapshot.activeVersionId)
                        ? "已保存当前版本"
                        : "保存当前版本"}
                </button>
              </div>
            ) : null}
            {saveExportMenu === "export" ? (
              <div className="toolbar-menu-popover export-menu" role="menu">
                <button
                  disabled={!plannerSnapshot.itineraryPlan || exportState === "exporting"}
                  onClick={() => {
                    setSaveExportMenu(null);
                    void handleExportMarkdown();
                  }}
                  role="menuitem"
                  type="button"
                >
                  导出 Markdown
                </button>
                <button
                  disabled={!plannerSnapshot.itineraryPlan || exportState === "exporting"}
                  onClick={() => {
                    setSaveExportMenu(null);
                    void handleExportJson();
                  }}
                  role="menuitem"
                  type="button"
                >
                  导出 JSON
                </button>
              </div>
            ) : null}
          </div>
        </div>
        </WorkspaceToolbar>
      </header>

      <WorkspacePanelSwitcher activePanel={activeWorkspacePanel} onChange={setActiveWorkspacePanel} />

      <section
        className="panel agent-panel workspace-pane workspace-pane-agent"
        aria-label="Agent conversation"
        id="workspace-pane-agent"
      >
        <header className="panel-header compact-tabs">
          <button type="button" className="tab active">
            对话
          </button>
          {!agentCollapsed ? (
            <div className="agent-panel-actions">
              <button
                aria-label="复制完整 Agent 调试上下文"
                disabled={
                  !hasConversationCopyContent(
                    plannerSnapshot.conversationTurns,
                    pendingAgentMessage,
                    pendingPlanningStep
                  )
                }
                onClick={() => void handleCopyDebug()}
                type="button"
              >
                {debugCopyState === "failed"
                  ? "复制失败"
                  : debugCopyState === "copied"
                    ? "完整上下文已复制"
                    : debugCopyState === "downloaded"
                      ? "内容过大，已下载完整调试包，剪贴板仅复制索引"
                      : "复制完整 Agent 调试上下文"}
              </button>
            </div>
          ) : null}
          <button
            aria-label={agentCollapsed ? "展开左侧对话" : "收缩左侧对话"}
            className="collapse-button"
            onClick={() => setAgentCollapsed((value) => !value)}
            type="button"
          >
            {agentCollapsed ? "›" : "‹"}
          </button>
        </header>
        <div className="agent-thread">
          <section className="agent-chat" aria-label="Agent messages" ref={agentChatRef} onScroll={chatFollow.onScroll}>
            <span aria-live="polite" className="sr-only" data-testid="message-copy-status">
              {messageCopyState?.status === "copied"
                ? `已复制消息 ${plannerSnapshot.conversationTurns.find((turn) => turn.id === messageCopyState.turnId)?.turnIndex ?? ""}`
                : messageCopyState?.status === "failed"
                  ? `复制消息 ${plannerSnapshot.conversationTurns.find((turn) => turn.id === messageCopyState.turnId)?.turnIndex ?? ""} 失败`
                  : ""}
            </span>
            {visibleConversationTurns.length ? (
              visibleConversationTurns.map((turn) => (
                <Fragment key={turn.id}>
                  <div
                    className={`chat-row ${turn.role === "assistant" ? "assistant" : "user"} ${turn.status}`}
                    data-assistant-response-group={turn.role === "assistant" ? turn.id : undefined}
                  >
                    {turn.role === "assistant" ? (
                      <div className="bot-avatar" aria-hidden="true">
                        AI
                      </div>
                    ) : null}
                    {turn.role === "assistant" && turn.reasoningStatuses?.length ? (
                      <AgentReasoningProgress live={false} statuses={turn.reasoningStatuses} />
                    ) : null}
                    {editingTurnId === turn.id ? (
                      <form
                        className="message-edit-form"
                        onSubmit={(event) => {
                          event.preventDefault();
                          void handleEditConversationTurn(turn.id);
                        }}
                      >
                        <label>
                          <span className="sr-only">编辑消息 {turn.turnIndex}</span>
                          <textarea
                            aria-label={`编辑消息 ${turn.turnIndex}`}
                            disabled={editingTurnSubmittingId === turn.id}
                            value={editingTurnContent}
                            onChange={(event) => setEditingTurnContent(event.target.value)}
                          />
                        </label>
                        <div>
                          <button disabled={editingTurnSubmittingId === turn.id} type="submit">
                            {editingTurnSubmittingId === turn.id ? "保存中" : "保存编辑"}
                          </button>
                          <button
                            disabled={editingTurnSubmittingId === turn.id}
                            type="button"
                            onClick={() => {
                              setEditingTurnId(null);
                              setEditingTurnContent("");
                            }}
                          >
                            取消
                          </button>
                        </div>
                      </form>
                    ) : (
                      <div className="message-bubble markdown-message">
                        <MarkdownMessage
                          content={
                            turn.role === "assistant" && turn.guideAdvice && !turn.guideAdvice.relevanceFilter
                              ? "这条历史普通攻略结果生成于新版相关性校验之前，旧摘要已隐藏；请重新搜索获取当前结果。"
                              : turn.content
                          }
                        />
                        {turn.status === "superseded" ? <span className="superseded-label">已被新分支取代</span> : null}
                      </div>
                    )}
                    {turn.role === "user" ? (
                      <div className="message-meta-actions">
                        <time dateTime={turn.createdAt}>
                          {new Date(turn.createdAt).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}
                        </time>
                        <button
                          aria-label={`复制消息 ${turn.turnIndex}`}
                          className={`message-icon-button ${messageCopyState?.turnId === turn.id ? messageCopyState.status : ""}`}
                          title={
                            messageCopyState?.turnId === turn.id
                              ? messageCopyState.status === "copied"
                                ? "已复制"
                                : "复制失败"
                              : "复制"
                          }
                          type="button"
                          onClick={() => void handleCopyConversationTurn(turn)}
                        >
                          <Copy aria-hidden="true" size={16} strokeWidth={1.8} />
                        </button>
                        {turn.status === "active" && editingTurnId !== turn.id ? (
                          <button
                            aria-label={`编辑消息 ${turn.turnIndex}`}
                            className="message-icon-button"
                            title="编辑"
                            type="button"
                            onClick={() => {
                              setEditingTurnId(turn.id);
                              setEditingTurnContent(turn.content);
                            }}
                          >
                            <Pencil aria-hidden="true" size={16} strokeWidth={1.8} />
                          </button>
                        ) : null}
                      </div>
                    ) : (
                      <time dateTime={turn.createdAt}>
                        {new Date(turn.createdAt).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}
                      </time>
                    )}
                    {turn.role === "assistant" && isBatchClarificationTurn(turn) ? (
                      <AgentClarificationBatch
                        capabilityCurrent={turn.id === currentClarificationTurnId}
                        checkpoint={turn.clarificationCheckpoint!}
                        disabled={isGenerating || turn.id !== currentClarificationTurnId}
                        executing={executingAgentChoiceKey === `${turn.id}:${batchSubmitOptionForTurn(turn)?.id ?? ""}`}
                        onSubmit={(selections) =>
                          handleClarificationBatchSubmit(batchSubmitOptionForTurn(turn)!, selections, turn)
                        }
                        sessionId={plannerSnapshot.agentSession?.sessionId ?? "pending-session"}
                        selectedMapPoi={plannerSnapshot.selectedMapPoi}
                        submission={turn.clarificationSubmission}
                        submitOption={batchSubmitOptionForTurn(turn)!}
                        turnId={turn.id}
                      />
                    ) : (
                      <>
                        {turn.role === "assistant" && turn.id === clarificationProgressTurnId ? (
                          <ClarificationProgress isCurrent={turn.id === currentClarificationTurnId} turn={turn} />
                        ) : null}
                        {turn.role === "assistant" && turn.status === "active" ? (
                          <ClarificationOptionButtons
                            activeComparisonPreview={plannerSnapshot.comparisonPreview}
                            disabled={isGenerating}
                            choiceLifecycleByKey={agentChoiceLifecycleByKey}
                            executingChoiceKey={executingAgentChoiceKey}
                            onCustomSelect={(option, value) =>
                              void handleClarificationCustomOptionSubmit(option, value, turn)
                            }
                            onSelect={(option) => void handleClarificationOptionSelect(option, turn)}
                            options={clarificationOptionsForTurn(turn).filter(
                              (option) =>
                                !turn.clarificationCheckpoint ||
                                turn.id === currentClarificationTurnId ||
                                !isClarificationCheckpointOption(option)
                            )}
                            turnId={turn.id}
                          />
                        ) : null}
                      </>
                    )}
                    {turn.role === "assistant" && turn.spatialBoundaryPreview ? (
                      <SpatialBoundaryPreviewCard preview={turn.spatialBoundaryPreview} />
                    ) : null}
                    {turn.role === "assistant" && turn.guideAdvice ? (
                      <TravelGuideAdviceCard advice={turn.guideAdvice} />
                    ) : null}
                    {turn.role === "assistant" && turn.sharedSource ? (
                      <SharedTravelSourceCard source={turn.sharedSource} />
                    ) : null}
                    {canResumeAgentTurn(turn, plannerSnapshot.activeVersionId) ? (
                      <button
                        className="message-edit-button"
                        disabled={isGenerating}
                        type="button"
                        onClick={() => void handleResumeAgentTurn(turn.id)}
                      >
                        继续
                      </button>
                    ) : null}
                    {turn.role === "assistant" ? (
                      <TurnPlanningEventsPanel
                        assistantTurnId={turn.id}
                        events={mergedTurnPlanningEvents(turn.planningSteps, turn.toolEvents)}
                        onExportTrace={handleExportPlanningTrace}
                        planningRunId={turn.planningRunId ?? null}
                        sessionId={plannerSnapshot.agentSession?.sessionId ?? null}
                        portfolioVisibility={
                          typeof turn.planningDirectionCount === "number" ||
                          typeof turn.visibleComparisonProposalCount === "number" ||
                          typeof turn.partialComparisonProposalCount === "number" ||
                          typeof turn.verifiedComparisonProposalCount === "number"
                            ? {
                                planningDirectionCount: turn.planningDirectionCount ?? 0,
                                visibleComparisonProposalCount: turn.visibleComparisonProposalCount ?? 0,
                                partialComparisonProposalCount: turn.partialComparisonProposalCount ?? 0,
                                verifiedComparisonProposalCount: turn.verifiedComparisonProposalCount ?? 0,
                                adoptionReadyProposalCount: turn.adoptionReadyProposalCount ?? 0
                              }
                            : null
                        }
                        title="查看详细执行记录"
                      />
                    ) : null}
                  </div>
                </Fragment>
              ))
            ) : (
              <div className="chat-row assistant">
                <div className="bot-avatar" aria-hidden="true">
                  AI
                </div>
                <div className="message-bubble">
                  你好！我是你的 AI 旅行规划师。直接告诉我旅行需求，我会生成并保存右侧行程。
                </div>
                <time>{lastQueried}</time>
              </div>
            )}
            {isGenerating ? (
              <>
                {pendingAgentMessage ? (
                  <div className="chat-row user pending">
                    <div className="message-bubble markdown-message">
                      <MarkdownMessage content={pendingAgentMessage} />
                    </div>
                    <time>发送中</time>
                  </div>
                ) : null}
                <div className="chat-row assistant streaming" data-assistant-response-group="live-agent-run">
                  <div className="bot-avatar" aria-hidden="true">
                    AI
                  </div>
                  <AgentReasoningProgress elapsedMs={agentRunElapsedMs} live statuses={liveReasoningStatuses} />
                </div>
                <button className="agent-stop-button" onClick={handleStopAgentRun} type="button">
                  停止本轮
                </button>
              </>
            ) : null}
            {!isGenerating && liveReasoningStatuses.length ? (
              <div className="chat-row assistant" data-assistant-response-group="interrupted-agent-run">
                <div className="bot-avatar" aria-hidden="true">
                  AI
                </div>
                <AgentReasoningProgress elapsedMs={agentRunElapsedMs} live={false} statuses={liveReasoningStatuses} />
              </div>
            ) : null}
            {extraction ? (
              <ExtractionReview
                cityCandidates={extraction.cityCandidates}
                poiCandidates={extraction.poiCandidates}
                styleTags={extraction.styleTags}
                budgetClues={extraction.budgetClues}
                routeClues={extraction.routeClues}
                confidence={extraction.confidence}
                sourceLinks={extraction.sourceLinks}
                needsUserConfirmation={extraction.needsUserConfirmation}
                providerName={extraction.providerName}
                fallbackUsed={extraction.fallbackUsed}
                providerFailureReason={extraction.providerFailureReason}
                userVisibleCaveat={extraction.userVisibleCaveat}
              />
            ) : null}
            {extraction ? <ItineraryDraft draft={extraction.itineraryDraft} /> : null}
          </section>
          <div className="composer-slot">
            {chatFollow.showLatest ? <button className="chat-latest-button" onClick={chatFollow.scrollLatest} type="button">回到最新消息 ↓</button> : null}
            {errorMessage ? <p role="alert">{errorMessage}</p> : null}
            {showSemanticHintActions ? (
              <div className="candidate-hint-actions" aria-label="候选提示操作">
                <button disabled={isGenerating} onClick={handleRetryCandidateHints} type="button">
                  让 Agent 自动补充候选
                </button>
                <button disabled={isGenerating} onClick={handleManualCandidateHints} type="button">
                  我来输入候选
                </button>
              </div>
            ) : null}
            <InspirationInput
              cityHint={selectedCity}
              disabled={isGenerating}
              onCityChange={(city) => plannerStore.setState({ selectedCity: city })}
              onSubmit={handleInspirationSubmit}
            />
          </div>
        </div>
      </section>

      <button
        aria-orientation="vertical"
        aria-valuemax={480}
        aria-valuemin={360}
        aria-valuenow={agentWidth}
        aria-label="调整左侧对话宽度"
        className="agent-resizer"
        onDoubleClick={() => setAgentWidth(DEFAULT_AGENT_PANEL_WIDTH)}
        onKeyDown={handleAgentResizeKeyDown}
        onPointerDown={handleAgentResizeStart}
        onPointerMove={handleAgentResizeMove}
        onPointerUp={handleAgentResizeEnd}
        role="separator"
        type="button"
      />

      <section
        className="map-surface workspace-pane workspace-pane-map"
        aria-label="3D map planning area"
        id="workspace-pane-map"
      >
        <PlannerMap
          plan={displayedPlan}
          selectedSegmentId={plannerSnapshot.selectedSegmentId}
          selectedDayNumber={plannerSnapshot.selectedDayNumber}
          selectedRouteOptionId={plannerSnapshot.selectedRouteOptionId}
          previewRouteOptionId={plannerSnapshot.previewRouteOptionId}
          onSelectSegment={handleSelectSegment}
          spatialBoundaryPreview={activeSpatialBoundaryPreview}
          onConfirmDensityCandidate={handleDensityMapCandidateConfirm}
          city={selectedCity}
          isActive={activeWorkspacePanel === "map"}
          interactionMode={plannerSnapshot.comparisonPreview.mapMode}
          routeColorOverrides={comparisonMapModel.routeColors}
          routeOpacityOverrides={comparisonMapModel.routeOpacities}
          segmentColorOverrides={comparisonMapModel.segmentColors}
          segmentOpacityOverrides={comparisonMapModel.segmentOpacities}
          comparisonLegend={comparisonMapModel.legend}
          onFocusComparisonPlan={handleComparisonPlanFocus}
          onDebugStateChange={(state) => {
            mapInteractionDebugRef.current = state;
          }}
        />
      </section>

      <button
        aria-orientation="vertical"
        aria-valuemax={580}
        aria-valuemin={520}
        aria-valuenow={timelineWidth}
        aria-label="调整地图和时间轴宽度"
        className="timeline-resizer"
        onDoubleClick={() => setTimelineWidth(DEFAULT_TIMELINE_PANEL_WIDTH)}
        onKeyDown={handleTimelineResizeKeyDown}
        onPointerDown={handleTimelineResizeStart}
        onPointerMove={handleTimelineResizeMove}
        onPointerUp={handleTimelineResizeEnd}
        role="separator"
        type="button"
      />

      <section
        className="panel itinerary-panel workspace-pane workspace-pane-timeline"
        aria-label="Daily itinerary timeline"
        id="workspace-pane-timeline"
      >
        <header aria-label="行程视图" className="panel-header compact-tabs" role="tablist">
          <button
            aria-controls="itinerary-overview-panel"
            aria-selected={activeItineraryTab === "overview"}
            type="button"
            className={`tab ${activeItineraryTab === "overview" ? "active" : ""}`}
            id="itinerary-tab-overview"
            onClick={() => void requestItineraryTabChange("overview")}
            onKeyDown={(event) => handleItineraryTabKeyDown(event, "overview")}
            role="tab"
            tabIndex={activeItineraryTab === "overview" ? 0 : -1}
          >
            行程总览
          </button>
          <button
            aria-controls="itinerary-comparison-panel"
            aria-selected={activeItineraryTab === "comparison"}
            type="button"
            className={`tab ${activeItineraryTab === "comparison" ? "active" : ""}`}
            id="itinerary-tab-comparison"
            onClick={() => void requestItineraryTabChange("comparison")}
            onKeyDown={(event) => handleItineraryTabKeyDown(event, "comparison")}
            role="tab"
            tabIndex={activeItineraryTab === "comparison" ? 0 : -1}
          >
            行程对比
          </button>
          <button
            aria-controls="itinerary-costs-panel"
            aria-selected={activeItineraryTab === "costs"}
            type="button"
            className={`tab ${activeItineraryTab === "costs" ? "active" : ""}`}
            id="itinerary-tab-costs"
            onClick={() => void requestItineraryTabChange("costs")}
            onKeyDown={(event) => handleItineraryTabKeyDown(event, "costs")}
            role="tab"
            tabIndex={activeItineraryTab === "costs" ? 0 : -1}
          >
            费用明细
          </button>
          <button
            aria-label={timelineCollapsed ? "展开右侧时间轴" : "收缩右侧时间轴"}
            className="collapse-button"
            onClick={() => setTimelineCollapsed((value) => !value)}
            type="button"
          >
            {timelineCollapsed ? "‹" : "›"}
          </button>
        </header>
        <div className="panel-body">
          {activeItineraryTab === "overview" ? (
            <section aria-labelledby="itinerary-tab-overview" id="itinerary-overview-panel" role="tabpanel">
              <DailyTimeline
                plan={displayedPlan}
                selectedDayNumber={plannerSnapshot.selectedDayNumber}
                selectedSegmentId={plannerSnapshot.selectedSegmentId}
                onSelectSegment={handleSelectSegment}
                onSelectPendingSlot={handlePendingTimelineSlotSelect}
                readOnly={plannerSnapshot.comparisonPreview.isMapReadOnly}
              />
              {comparisonError ? (
                <p className="timeline-error" role="alert">
                  {comparisonError}
                </p>
              ) : null}
            </section>
          ) : null}
          {activeItineraryTab === "comparison" ? (
            plannerSnapshot.comparisonPreview.plans.length ||
            plannerSnapshot.comparisonPreview.comparisonSummary ||
            plannerSnapshot.planComparison ||
            comparisonError ? (
              <section aria-labelledby="itinerary-tab-comparison" id="itinerary-comparison-panel" role="tabpanel">
                <PlanComparison
                  comparison={plannerSnapshot.planComparison}
                  errorMessage={comparisonError}
                  preview={plannerSnapshot.comparisonPreview}
                  focusedDayNumber={plannerSnapshot.selectedDayNumber}
                  onFocusPlan={handleComparisonPlanFocus}
                  onFocusPlanDay={handleComparisonPlanFocus}
                  onOpenPlanDetails={handleComparisonPlanDetails}
                  onAdoptPlan={handleComparisonPlanAdopt}
                  onCompleteTheme={handleComparisonThemeCompletion}
                  onRepairPlan={handleComparisonPlanRepair}
                  adoptingChoiceId={executingAgentChoiceKey?.split(":").slice(-1)[0] ?? null}
                  completingChoiceId={executingAgentChoiceKey?.split(":").slice(-1)[0] ?? null}
                />
              </section>
            ) : (
              <section
                aria-labelledby="itinerary-tab-comparison"
                className="timeline-empty-state"
                id="itinerary-comparison-panel"
                role="tabpanel"
              >
                <h2>行程对比待接入</h2>
                <p>生成多个对比方案后将在这里展示各方案的差异和决策依据。</p>
              </section>
            )
          ) : null}
          {activeItineraryTab === "costs" ? (
            <CostBreakdownPanel
              id="itinerary-costs-panel"
              labelledBy="itinerary-tab-costs"
              plan={plannerSnapshot.itineraryPlan}
            />
          ) : null}
        </div>
      </section>
    </main>
  );
}

function comparisonProjectionToItinerary(projection: ComparisonPlanProjection): ItineraryPlan {
  return {
    id: `preview:${projection.proposalId}`,
    title: projection.title,
    city: "",
    templateType: projection.isPartial ? "portfolio_partial" : "creative_portfolio",
    budgetTarget: null,
    budgetTier: "unknown",
    budgetEstimate: 0,
    budgetDeltaExplanation: projection.budgetSummary,
    decisionRationale: projection.tradeoffSummary,
    status: projection.status,
    days: projection.days.map((day) => ({
      ...day,
      weatherSummary: day.weatherSummary ?? "待查询",
      riskSummary: day.riskSummary ?? "待核验",
      totalEstimatedCost: day.totalEstimatedCost ?? 0
    })),
    routeOptions: projection.routeEvidence,
    weatherSignals: [],
    trafficCrowdingSignals: [],
    poiRiskAlerts: [],
    ticketLookupResults: [],
    routeWarnings: projection.isPartial ? ["部分方案只显示已验证路线；缺口不会补画。"] : []
  };
}

const COMPARISON_COLORS: Record<string, string> = {
  ocean: "#2563eb",
  amber: "#b45309",
  violet: "#7c3aed",
  teal: "#0f766e",
  rose: "#be123c",
  indigo: "#4338ca",
  lime: "#4d7c0f",
  slate: "#475569"
};

function comparisonPreviewMapModel(preview: ReturnType<typeof plannerStore.getSnapshot>["comparisonPreview"]) {
  const routeColors: Record<string, string> = {};
  const routeOpacities: Record<string, number> = {};
  const segmentColors: Record<string, string> = {};
  const segmentOpacities: Record<string, number> = {};
  const days = new Map<number, ItineraryPlan["days"][number]>();
  const routes: ItineraryPlan["routeOptions"] = [];
  for (const projection of preview.plans) {
    const prefix = `${projection.proposalId}:`;
    for (const day of projection.days) {
      const target = days.get(day.dayNumber) ?? {
        id: `comparison-day-${day.dayNumber}`,
        dayNumber: day.dayNumber,
        title: `Day ${day.dayNumber}`,
        date: day.date,
        weatherSummary: "只读方案对比",
        riskSummary: "仅显示真实高德地点",
        totalEstimatedCost: 0,
        segments: [],
        pendingSlots: []
      };
      target.segments.push(
        ...day.segments.map((segment) => {
          const id = `${prefix}${segment.id}`;
          segmentColors[id] = COMPARISON_COLORS[projection.colorKey] ?? COMPARISON_COLORS.ocean;
          segmentOpacities[id] = preview.focusedProposalId === projection.proposalId ? 1 : 0.42;
          return { ...segment, id };
        })
      );
      target.pendingSlots?.push(
        ...(day.pendingSlots ?? []).map((slot) => ({
          ...slot,
          id: `${prefix}${slot.id}`,
          planningSlotId: `${prefix}${slot.planningSlotId}`
        }))
      );
      days.set(day.dayNumber, target);
    }
    for (const route of projection.routeEvidence) {
      const id = `${prefix}${route.id}`;
      routes.push({
        ...route,
        id,
        fromSegmentId: route.fromSegmentId ? `${prefix}${route.fromSegmentId}` : route.fromSegmentId,
        toSegmentId: route.toSegmentId ? `${prefix}${route.toSegmentId}` : route.toSegmentId
      });
      routeColors[id] = COMPARISON_COLORS[projection.colorKey] ?? COMPARISON_COLORS.ocean;
      routeOpacities[id] = preview.focusedProposalId === projection.proposalId ? 0.95 : 0.28;
    }
  }
  const first = preview.plans[0];
  return {
    plan: first
      ? {
          ...comparisonProjectionToItinerary(first),
          id: `comparison:${preview.planningSelectionRootTurnId ?? "unknown"}`,
          title: "多方案地图对比",
          days: [...days.values()].sort((left, right) => left.dayNumber - right.dayNumber),
          routeOptions: routes
        }
      : null,
    routeColors,
    routeOpacities,
    segmentColors,
    segmentOpacities,
    legend: preview.plans.map((plan, index) => ({
      id: plan.proposalId,
      label: `方案 ${index + 1}：${plan.title}`,
      color: COMPARISON_COLORS[plan.colorKey] ?? COMPARISON_COLORS.ocean,
      focused: preview.focusedProposalId === plan.proposalId
    }))
  };
}

function selectedRouteForSegment(
  plan: ReturnType<typeof plannerStore.getSnapshot>["itineraryPlan"],
  segmentId: string | null
) {
  if (!plan || !segmentId) {
    return null;
  }
  const day = plan.days.find((item) => item.segments.some((segment) => segment.id === segmentId));
  const segmentIndex = day?.segments.findIndex((segment) => segment.id === segmentId) ?? -1;
  const current = day?.segments[segmentIndex];
  const next = day ? day.segments.slice(segmentIndex + 1).find(isRouteAnchorSegment) : undefined;
  if (!current || !isRouteAnchorSegment(current) || !next) {
    return null;
  }
  const candidates = plan.routeOptions.filter(
    (route) => route.fromSegmentId === current.id && route.toSegmentId === next.id
  );
  return candidates.find((route) => route.isSelected) ?? fastestRoute(candidates);
}

function downloadTextFile(filename: string, content: string, type: string) {
  const blob = new Blob([content], { type });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

function safeFilename(value: string) {
  return (
    value
      .replace(/[\\/:*?"<>|]+/g, "-")
      .replace(/\s+/g, "-")
      .slice(0, 80) || "trip-itinerary"
  );
}

function fastestRoute<
  T extends {
    durationSeconds?: number;
    durationMinutes?: number;
    costAmount?: number | null;
    costEstimate?: number;
    sortOrder?: number;
  }
>(routes: T[]) {
  return (
    [...routes].sort((left, right) => {
      const durationDiff = routeDurationSeconds(left) - routeDurationSeconds(right);
      if (durationDiff !== 0) {
        return durationDiff;
      }
      const costDiff = (left.costAmount ?? left.costEstimate ?? 0) - (right.costAmount ?? right.costEstimate ?? 0);
      if (costDiff !== 0) {
        return costDiff;
      }
      return (left.sortOrder ?? 0) - (right.sortOrder ?? 0);
    })[0] ?? null
  );
}

function routeDurationSeconds(route: { durationSeconds?: number; durationMinutes?: number }) {
  if (route.durationSeconds && route.durationSeconds > 0) {
    return route.durationSeconds;
  }
  return (route.durationMinutes ?? 0) * 60;
}

function hasActualPreferenceCard(card: ReturnType<typeof defaultPreferenceCard>) {
  return Boolean(effectivePreferenceMemoryText(card.summaryText) || card.items.some((item) => item.label.trim()));
}

type ClarificationOption = {
  id?: string;
  index: number;
  displayIndex?: number;
  label: string;
  value?: string;
  semanticValue?: string | Record<string, unknown>;
  dimensionId?: string;
  checkpointId?: string;
  checkpointFingerprint?: string;
  kind?: string;
  action?: AgentChoiceOption["action"];
  scopeKind?: AgentChoiceOption["scopeKind"];
  lifecycle?: AgentChoiceOption["lifecycle"];
  allowsManualInput?: boolean;
  attempt?: number;
  localPoiScope?: string;
  candidateRecordId?: string;
  amapId?: string;
  segmentId?: string;
  amapPoi?: MapPoi;
  selectionGroupId?: string;
  briefId?: string;
  planningSlotId?: string;
  poolId?: string;
  dayNumber?: number;
  densityGroups?: Array<Record<string, unknown>>;
  timeWindow?: string;
  displayNeed?: string;
  expectedBaseVersionId?: string;
  planningSelectionRootTurnId?: string;
  rootPortfolioId?: string;
  focusBriefId?: string;
  requestContractFingerprint?: string;
  comparisonAnchors?: Array<MapPoi & { startTime?: string | null; timeWindow?: string | null }>;
};

function activeClarificationTurnId(turns: ConversationTurn[]): string | null {
  const answeredSourceTurnIds = new Set(
    turns
      .filter((turn) => turn.role === "user" && turn.structuredChoiceTrace?.executionStatus === "succeeded")
      .map((turn) => String(turn.structuredChoiceTrace?.sourceAssistantTurnId ?? "").trim())
      .filter(Boolean)
  );
  const current = [...turns].reverse().find((turn) => {
    if (
      turn.role !== "assistant" ||
      turn.status !== "active" ||
      !turn.clarificationCheckpoint ||
      answeredSourceTurnIds.has(turn.id)
    ) {
      return false;
    }
    const status = String(turn.clarificationCheckpoint.status ?? "").toLowerCase();
    const hasPendingQuestion = Boolean(
      turn.clarificationCheckpoint.nextQuestionDimensionId || turn.clarificationCheckpoint.question?.question
    );
    const isWaiting = ["", "active", "awaiting_answer", "awaiting_agent_resolution"].includes(status);
    const hasOfferedChoice = (turn.choiceOptions ?? []).some(
      (option) =>
        isClarificationCheckpointOption(normalizeStructuredOptions([option])[0]) &&
        !["consumed", "failed_terminal", "expired", "stale", "cancelled"].includes(
          String(option.lifecycle ?? "offered")
        )
    );
    return (isWaiting || hasPendingQuestion) && hasOfferedChoice;
  });
  return current?.id ?? null;
}

function latestClarificationCheckpointTurnId(turns: ConversationTurn[]): string | null {
  return (
    [...turns]
      .reverse()
      .find((turn) => turn.role === "assistant" && turn.status === "active" && Boolean(turn.clarificationCheckpoint))
      ?.id ?? null
  );
}

function isClarificationCheckpointOption(option?: ClarificationOption | null): boolean {
  return Boolean(
    option &&
    (option.action === "continue_clarification" ||
      option.action === "submit_clarification_batch" ||
      option.action === "select_spatial_boundary_candidate" ||
      option.action === "confirm_spatial_boundary" ||
      option.action === "change_spatial_boundary" ||
      option.action === "retry_spatial_grounding" ||
      option.kind === "clarification_checkpoint" ||
      option.kind === "clarification_batch_submit" ||
      option.kind === "spatial_boundary_confirmation" ||
      option.kind === "spatial_boundary_change" ||
      option.kind === "spatial_grounding_retry" ||
      option.kind === "custom_input")
  );
}

function batchSubmitOptionForTurn(turn: ConversationTurn): ClarificationOption | null {
  return (
    normalizeStructuredOptions(turn.choiceOptions).find(
      (option) => option.action === "submit_clarification_batch" && option.kind === "clarification_batch_submit"
    ) ?? null
  );
}

function isBatchClarificationTurn(turn: ConversationTurn): boolean {
  const checkpoint = turn.clarificationCheckpoint;
  const submit = batchSubmitOptionForTurn(turn);
  const questions = checkpoint?.questions ?? [];
  const dimensionIds = questions.map((question) => question.dimensionId.trim());
  const questionsValid =
    questions.length >= 1 &&
    questions.length <= 3 &&
    dimensionIds.every(Boolean) &&
    new Set(dimensionIds).size === dimensionIds.length &&
    questions.every((question) => {
      const optionIds = question.options.map((option) => option.id.trim());
      return (
        question.question.trim().length > 0 &&
        question.whyItMatters.trim().length > 0 &&
        optionIds.length >= 2 &&
        optionIds.length <= 3 &&
        optionIds.every(Boolean) &&
        new Set(optionIds).size === optionIds.length &&
        question.options.every((option) => option.label.trim().length > 0)
      );
    });
  return Boolean(
    turn.status === "active" &&
    checkpoint?.schemaVersion === "clarification-checkpoint-v2" &&
    checkpoint.submissionMode === "batch_atomic" &&
    checkpoint.sourceAssistantTurnId === turn.id &&
    checkpoint.fingerprint?.trim() &&
    checkpoint.submitChoiceId === submit?.id &&
    submit?.checkpointId === checkpoint.checkpointId &&
    submit?.checkpointFingerprint === checkpoint.fingerprint &&
    questionsValid
  );
}

function ClarificationProgress({ isCurrent, turn }: { isCurrent: boolean; turn: ConversationTurn }) {
  const checkpoint = turn.clarificationCheckpoint;
  if (!checkpoint) {
    return null;
  }
  const progress = clarificationProgressModel(turn, checkpoint);
  return (
    <section
      aria-label="澄清进度"
      aria-live="polite"
      className="clarification-option-list"
      data-checkpoint-id={checkpoint.checkpointId}
      role="status"
    >
      <p>
        <strong>
          澄清进度：已确认 {progress.confirmedCount}
          {progress.totalCount > 0 ? `/${progress.totalCount}` : ""} 项关键约束
        </strong>
        {progress.experienceSpecCount > 0 ? `，已形成 ${progress.experienceSpecCount} 项可执行体验约束。` : "。"}
      </p>
      {isCurrent && progress.currentQuestion ? (
        <p>
          <strong>当前只需回答：</strong>
          {progress.currentQuestion}
        </p>
      ) : null}
      <p>
        <strong>待补缺口：</strong>
        {progress.gapSummary}
      </p>
    </section>
  );
}

function clarificationProgressModel(turn: ConversationTurn, checkpoint: ClarificationCheckpoint) {
  const answers = [...(checkpoint.resolvedAnswers ?? []), ...(checkpoint.answers ?? [])];
  const confirmedDimensionIds = new Set(
    [
      ...answers.map((item) => item.dimensionId),
      ...(checkpoint.resolvedDimensions ?? []),
      ...(checkpoint.ambiguities ?? []).filter((item) => item.resolved === true).map((item) => item.dimensionId)
    ]
      .map((item) => String(item ?? "").trim())
      .filter(Boolean)
  );
  const unresolvedDimensionIds = new Set(
    [
      ...(checkpoint.unresolvedDimensions ?? []),
      ...(checkpoint.ambiguities ?? []).filter((item) => item.resolved !== true).map((item) => item.dimensionId),
      checkpoint.nextQuestionDimensionId ?? ""
    ]
      .map((item) => String(item ?? "").trim())
      .filter(Boolean)
  );
  for (const dimensionId of confirmedDimensionIds) {
    unresolvedDimensionIds.delete(dimensionId);
  }
  const totalDimensionIds = new Set([...confirmedDimensionIds, ...unresolvedDimensionIds]);
  const experienceSpecs = checkpoint.experienceSpecs?.length
    ? checkpoint.experienceSpecs
    : (turn.experienceSpecs ?? []);
  const checkpointGapSummary = checkpoint.candidateGapSummary ?? {};
  const candidateGapSummary: Record<string, unknown> = Object.keys(checkpointGapSummary).length
    ? checkpointGapSummary
    : (turn.candidateGapSummary ?? {});
  const gapCount = candidateGapCount(candidateGapSummary);
  const gapSummary =
    gapCount == null
      ? Object.keys(candidateGapSummary).length > 0
        ? "真实地点或路线证据仍需补齐。"
        : isCheckpointComplete(checkpoint, unresolvedDimensionIds.size)
          ? "约束已收敛，可继续核验真实地点和路线。"
          : "待本轮回答后继续核验真实地点和路线。"
      : gapCount > 0
        ? `还需补齐 ${gapCount} 个规划目标的真实地点与路线证据。`
        : "所需真实地点与路线证据已补齐。";
  return {
    confirmedCount: confirmedDimensionIds.size,
    totalCount: totalDimensionIds.size,
    experienceSpecCount: experienceSpecs.length,
    currentQuestion: String(checkpoint.question?.question ?? turn.content ?? "").trim(),
    gapSummary
  };
}

function isCheckpointComplete(checkpoint: ClarificationCheckpoint, unresolvedCount: number): boolean {
  const status = String(checkpoint.status ?? "").toLowerCase();
  return unresolvedCount === 0 && ["resolved", "complete", "completed"].includes(status);
}

function candidateGapCount(summary: Record<string, unknown>): number | null {
  const fields = numericSummaryFields(summary);
  const explicit = fields.find(({ key }) =>
    /(missing|remaining|pending|shortfall|gap|unresolved).*(count|occurrence|slot|goal)?/i.test(key)
  );
  if (explicit) {
    return Math.max(0, Math.round(explicit.value));
  }
  const target = fields.find(
    ({ key }) =>
      /(target|required|needed|slot)/i.test(key) &&
      !/(grounded|admitted|covered|fulfilled|accepted|available)/i.test(key)
  );
  if (!target) {
    return null;
  }
  const covered = fields.find(({ key }) => /(grounded|admitted|covered|fulfilled|accepted|available)/i.test(key));
  return Math.max(0, Math.round(target.value - (covered?.value ?? 0)));
}

function numericSummaryFields(value: unknown, path = "", depth = 0): Array<{ key: string; value: number }> {
  if (depth > 4 || value == null) {
    return [];
  }
  if (typeof value === "number" && Number.isFinite(value)) {
    return [{ key: path, value }];
  }
  if (Array.isArray(value)) {
    const arrayCount = /(missing|remaining|pending|shortfall|gap|blocker)/i.test(path)
      ? [{ key: `${path}Count`, value: value.length }]
      : [];
    return [
      ...arrayCount,
      ...value.flatMap((item, index) => numericSummaryFields(item, `${path}[${index}]`, depth + 1))
    ];
  }
  if (typeof value !== "object") {
    return [];
  }
  return Object.entries(value as Record<string, unknown>).flatMap(([key, nested]) =>
    numericSummaryFields(nested, path ? `${path}.${key}` : key, depth + 1)
  );
}

function isOutdatedComparisonChoice(option: ClarificationOption, preview: PlanComparisonPreviewState): boolean {
  if (
    option.scopeKind === "clarification" ||
    option.action === "continue_clarification" ||
    option.action === "submit_clarification_batch"
  ) {
    return false;
  }
  const hasComparisonScope = Boolean(
    option.scopeKind === "comparison" ||
    isComparisonChoiceAction(option.action) ||
    (!option.scopeKind && option.rootPortfolioId && option.planningSelectionRootTurnId)
  );
  const hasActiveComparisonScope = Boolean(preview.planningSelectionRootTurnId && preview.rootPortfolioId);
  return hasComparisonScope && hasActiveComparisonScope && !isCurrentComparisonScope(preview, option);
}

function isComparisonChoiceAction(action: AgentChoiceOption["action"]): boolean {
  return new Set<AgentChoiceOption["action"]>([
    "select_plan_proposal",
    "manual_continuation",
    "retry_model_planning",
    "attempt_portfolio_theme_completion",
    "confirm_portfolio_theme_upgrade",
    "confirm_portfolio_theme_replacement",
    "reject_portfolio_theme_replacement",
    "resume_density_candidate",
    "refresh_density_candidates",
    "expand_density_nearby",
    "open_density_map"
  ]).has(action);
}

function clarificationOptionsForTurn(turn: ConversationTurn): ClarificationOption[] {
  return structuredClarificationOptions(turn).map((option, index) => ({ ...option, displayIndex: index + 1 }));
}

function structuredClarificationOptions(turn: ConversationTurn): ClarificationOption[] {
  // A portfolio_comparison_readonly option is a server-authored projection
  // carrier for the comparison panel, not a chat capability. Keeping it out
  // of clarification buttons prevents a user from attempting to execute an
  // action-less card while preserving its authoritative route/readiness data.
  const comparisonScope = comparisonScopeFromTurn(turn);
  const choiceOptions = normalizeStructuredOptions(turn.choiceOptions)
    .map((option) => inheritComparisonScope(option, comparisonScope))
    .filter((option) => option.kind !== "portfolio_comparison_readonly" && Boolean(option.id));
  if (choiceOptions.length >= 1 && choiceOptions.every(isRenderableStructuredChoice)) {
    return choiceOptions;
  }
  if (choiceOptions.length >= 2) {
    return ensureCustomOption(choiceOptions);
  }
  const localOptions = turn.localPoiOptions;
  if (!localOptions || !Array.isArray(localOptions.options)) {
    return [];
  }
  const options = normalizeStructuredOptions(localOptions.options)
    .filter((option) => Boolean(option.id))
    .map((option) => ({
      ...option,
      localPoiScope: String(localOptions.scope ?? "")
    }));
  if (options.length < 1) {
    return [];
  }
  const includeCustom = localOptions.includeCustomOption !== false;
  return includeCustom ? ensureCustomOption(options) : options;
}

function comparisonScopeFromTurn(
  turn: ConversationTurn
): Pick<ClarificationOption, "planningSelectionRootTurnId" | "rootPortfolioId"> | null {
  const candidates = [
    ...(Array.isArray(turn.comparisonProjections) ? turn.comparisonProjections : []),
    ...(Array.isArray(turn.choiceOptions) ? turn.choiceOptions.map((option) => option.comparisonProjection) : [])
  ];
  for (const candidate of candidates) {
    if (!candidate || typeof candidate !== "object") continue;
    const record = candidate as Record<string, unknown>;
    const planningSelectionRootTurnId = optionalString(record.planningSelectionRootTurnId);
    const rootPortfolioId = optionalString(record.rootPortfolioId);
    if (planningSelectionRootTurnId && rootPortfolioId) {
      return { planningSelectionRootTurnId, rootPortfolioId };
    }
  }
  return null;
}

function inheritComparisonScope(
  option: ClarificationOption,
  scope: Pick<ClarificationOption, "planningSelectionRootTurnId" | "rootPortfolioId"> | null
): ClarificationOption {
  if (!scope || !String(option.kind ?? "").startsWith("portfolio_")) {
    return option;
  }
  return {
    ...option,
    planningSelectionRootTurnId: option.planningSelectionRootTurnId ?? scope.planningSelectionRootTurnId,
    rootPortfolioId: option.rootPortfolioId ?? scope.rootPortfolioId
  };
}

function isExecutableStructuredChoice(option: ClarificationOption) {
  return Boolean(
    option.id &&
    option.action &&
    !["consumed", "failed_terminal", "expired", "stale", "cancelled"].includes(String(option.lifecycle ?? "offered"))
  );
}

function normalizeStructuredOptions(rawOptions: AgentChoiceOption[] | unknown): ClarificationOption[] {
  if (!Array.isArray(rawOptions)) {
    return [];
  }
  return rawOptions
    .map((rawOption, optionIndex): ClarificationOption | null => {
      if (!rawOption || typeof rawOption !== "object") {
        return null;
      }
      const record = rawOption as Record<string, unknown>;
      const label = String(record.label ?? record.value ?? record.title ?? "").trim();
      if (!label) {
        return null;
      }
      const indexValue = Number(record.index ?? optionIndex + 1);
      const kind = String((record.kind ?? (record.custom ? "custom_input" : "")) || "");
      return {
        id: optionalString(record.id),
        index: Number.isFinite(indexValue) && indexValue > 0 ? indexValue : optionIndex + 1,
        label,
        value: optionalString(record.value),
        semanticValue:
          typeof record.semanticValue === "string"
            ? record.semanticValue
            : record.semanticValue && typeof record.semanticValue === "object"
              ? (record.semanticValue as Record<string, unknown>)
              : undefined,
        dimensionId: optionalString(record.dimensionId),
        checkpointId: optionalString(record.checkpointId),
        checkpointFingerprint: optionalString(record.checkpointFingerprint),
        kind: kind || undefined,
        action: record.action as AgentChoiceOption["action"],
        scopeKind: record.scopeKind as AgentChoiceOption["scopeKind"],
        lifecycle: record.lifecycle as AgentChoiceOption["lifecycle"],
        allowsManualInput: typeof record.allowsManualInput === "boolean" ? record.allowsManualInput : undefined,
        attempt: typeof record.attempt === "number" ? record.attempt : undefined,
        candidateRecordId: optionalString(record.candidateRecordId),
        amapId: optionalString(record.amapId),
        segmentId: optionalString(record.segmentId),
        amapPoi: isMapPoi(record.amapPoi) ? record.amapPoi : undefined,
        selectionGroupId: optionalString(record.selectionGroupId),
        briefId: optionalString(record.briefId),
        planningSlotId: optionalString(record.planningSlotId),
        poolId: optionalString(record.poolId),
        dayNumber: typeof record.dayNumber === "number" ? record.dayNumber : undefined,
        densityGroups: Array.isArray(record.densityGroups)
          ? record.densityGroups.filter(
              (item): item is Record<string, unknown> => Boolean(item) && typeof item === "object"
            )
          : undefined,
        timeWindow: optionalString(record.timeWindow),
        displayNeed: optionalString(record.displayNeed),
        expectedBaseVersionId: optionalString(record.expectedBaseVersionId),
        planningSelectionRootTurnId: optionalString(record.planningSelectionRootTurnId),
        rootPortfolioId: optionalString(record.rootPortfolioId),
        focusBriefId: optionalString(record.focusBriefId),
        requestContractFingerprint: optionalString(record.requestContractFingerprint),
        comparisonAnchors: Array.isArray(record.comparisonAnchors)
          ? record.comparisonAnchors
              .filter((item): item is MapPoi => isMapPoi(item))
              .map((item) => ({
                ...item,
                startTime: optionalString((item as Record<string, unknown>).startTime),
                timeWindow: optionalString((item as Record<string, unknown>).timeWindow)
              }))
          : undefined
      };
    })
    .filter((option): option is ClarificationOption => Boolean(option));
}

function optionalString(value: unknown) {
  const normalized = typeof value === "string" ? value.trim() : "";
  return normalized || undefined;
}

function isMapPoi(value: unknown): value is MapPoi {
  if (!value || typeof value !== "object") {
    return false;
  }
  const poi = value as Partial<MapPoi>;
  return Boolean(poi.id && poi.name && Number.isFinite(poi.longitude) && Number.isFinite(poi.latitude));
}

function isPortfolioRouteQualityError(error: unknown): error is ApiError {
  if (!(error instanceof ApiError)) {
    return false;
  }
  const envelope = error.details && typeof error.details === "object" ? (error.details as Record<string, unknown>) : {};
  if (error.code === "plan_proposal_route_quality_failed" || envelope.code === "plan_proposal_route_quality_failed") {
    return true;
  }
  const details =
    envelope.details && typeof envelope.details === "object" ? (envelope.details as Record<string, unknown>) : envelope;
  const hardFailures = Array.isArray(details.hardFailures) ? details.hardFailures.map((item) => String(item)) : [];
  const routeQualityIssues = Array.isArray(details.routeQualityIssues) ? details.routeQualityIssues : [];
  return (
    (error.code === "AGENT_VERIFIER_FAILED" || envelope.code === "AGENT_VERIFIER_FAILED") &&
    (routeQualityIssues.length > 0 || hardFailures.some((item) => /route_quality|meal_detour/i.test(item)))
  );
}

function portfolioRouteQualityMessage(error: ApiError) {
  const envelope = error.details && typeof error.details === "object" ? (error.details as Record<string, unknown>) : {};
  const details =
    envelope.details && typeof envelope.details === "object" ? (envelope.details as Record<string, unknown>) : envelope;
  const issues = Array.isArray(details.routeQualityIssues) ? details.routeQualityIssues : [];
  const firstIssue = issues.find((item) => item && typeof item === "object") as Record<string, unknown> | undefined;
  const location = firstIssue
    ? `${String(firstIssue.fromPoiName ?? "")} → ${String(firstIssue.toPoiName ?? "")}`.replace(
        /^\s*→\s*|\s*→\s*$/g,
        ""
      )
    : "相邻路线";
  const distance = firstIssue?.distanceKm != null ? `，约 ${String(firstIssue.distanceKm)} km` : "";
  const duration = firstIssue?.durationMinutes != null ? `、${String(firstIssue.durationMinutes)} 分钟` : "";
  const actions = Array.isArray(details.recommendedNextActions) ? details.recommendedNextActions : [];
  const hardFailures = Array.isArray(details.hardFailures) ? details.hardFailures.map((item) => String(item)) : [];
  const next =
    actions.includes("open_map_selection") || actions.includes("choose_nearby_meal")
      ? "可改选附近餐饮或打开地图选择后重试。"
      : "请重试路线核验。";
  const failure = hardFailures.find((item) => /meal_detour/i.test(item));
  const prefix = firstIssue
    ? `${location}餐饮绕行不满足路线质量要求${distance}${duration}。`
    : failure
      ? "餐饮与相邻地点的绕行不满足路线质量要求。"
      : "路线质量校验未通过。";
  return `${prefix}${next}`;
}

function isPlanProposalExpiredError(error: unknown): error is ApiError {
  if (!(error instanceof ApiError)) {
    return false;
  }
  const envelope = error.details && typeof error.details === "object" ? (error.details as Record<string, unknown>) : {};
  return error.code === "plan_proposal_expired" || envelope.code === "plan_proposal_expired";
}

function isPlanProposalRequestScopeInvalidError(error: unknown): error is ApiError {
  if (!(error instanceof ApiError)) {
    return false;
  }
  const envelope = error.details && typeof error.details === "object" ? (error.details as Record<string, unknown>) : {};
  return error.code === "plan_proposal_request_scope_invalid" || envelope.code === "plan_proposal_request_scope_invalid";
}

function isRefreshableAgentChoiceError(error: unknown) {
  if (!(error instanceof ApiError) || ![409, 422].includes(error.status)) {
    return false;
  }
  const envelope = error.details && typeof error.details === "object" ? (error.details as Record<string, unknown>) : {};
  const code = String(envelope.code ?? error.code ?? "").toLowerCase();
  return [
    "agent_choice_identity_mismatch",
    "agent_choice_target_stale",
    "agent_choice_invalid",
    "agent_choice_source_context_missing",
    "plan_proposal_request_scope_invalid",
    "portfolio_partial_timeline_missing",
    "candidate_identity_mismatch",
    "base_version_conflict",
    "stale_base_version",
    "stale_agent_choice"
  ].includes(code);
}

function ensureCustomOption(options: ClarificationOption[]): ClarificationOption[] {
  const regularOptions = options.filter((option) => !isCustomClarificationOption(option));
  const densityOptions = options.filter(
    (option) =>
      String(option.kind ?? "").startsWith("portfolio_density_") ||
      Boolean(option.briefId && option.poolId && option.planningSlotId && option.dayNumber)
  );
  if (densityOptions.length > 0) {
    return options.map((option, index) => ({ ...option, index: index + 1 }));
  }
  const firstGroupId = regularOptions.find((option) => option.selectionGroupId)?.selectionGroupId;
  const groupedOptions = firstGroupId
    ? regularOptions.filter((option) => option.selectionGroupId === firstGroupId)
    : regularOptions;
  const custom = options.find(
    (option) =>
      isCustomClarificationOption(option) &&
      (!firstGroupId || !option.selectionGroupId || option.selectionGroupId === firstGroupId)
  );
  return [...groupedOptions.slice(0, 3), ...(custom ? [custom] : [])].map((option, index) => ({
    ...option,
    index: index + 1
  }));
}

function ClarificationOptionButtons({
  activeComparisonPreview,
  disabled,
  choiceLifecycleByKey,
  executingChoiceKey,
  onCustomSelect,
  onSelect,
  options,
  turnId
}: {
  activeComparisonPreview: PlanComparisonPreviewState;
  disabled: boolean;
  choiceLifecycleByKey: Record<string, AgentChoiceOption["lifecycle"]>;
  executingChoiceKey: string | null;
  onCustomSelect: (option: ClarificationOption, value: string) => void;
  onSelect: (option: ClarificationOption) => void;
  options: ClarificationOption[];
  turnId: string;
}) {
  const [customValues, setCustomValues] = useState<Record<string, string>>({});
  const isDensityChoiceSet = options.some(
    (option) =>
      String(option.kind ?? "").startsWith("portfolio_density_") ||
      Boolean(option.briefId && option.poolId && option.planningSlotId && option.dayNumber)
  );
  const densityCandidateCount = options.filter(
    (option) => option.action === "resume_density_candidate" && Boolean(option.amapId)
  ).length;
  const hasExecutableStructuredChoice = options.some(isExecutableStructuredChoice);
  const comparisonConfirmOptions = options.filter((option) => option.action === "select_plan_proposal");
  const comparisonContinuationOptions = options.filter(
    (option) =>
      option.action !== "select_plan_proposal" &&
      isComparisonChoiceAction(option.action) &&
      (option.scopeKind === "comparison" ||
        (!option.scopeKind && Boolean(option.rootPortfolioId && option.planningSelectionRootTurnId)))
  );
  const comparisonOptionIds = new Set(
    [...comparisonConfirmOptions, ...comparisonContinuationOptions].map((option) => option.id)
  );
  const displayOptions = options.filter((option) => !comparisonOptionIds.has(option.id));
  if (
    options.length < 1 ||
    (options.length < 2 && !hasExecutableStructuredChoice && !options.every(isRenderableStructuredChoice))
  ) {
    return null;
  }
  return (
    <>
      {comparisonConfirmOptions.length || comparisonContinuationOptions.length ? (
        <PlanComparisonActions
          activeComparisonPreview={activeComparisonPreview}
          choiceLifecycleByKey={choiceLifecycleByKey}
          disabled={disabled}
          executingChoiceKey={executingChoiceKey}
          onSelect={onSelect}
          options={[...comparisonConfirmOptions, ...comparisonContinuationOptions]}
          turnId={turnId}
        />
      ) : null}
      {displayOptions.length ? (
        <div
          className={`clarification-option-list ${isDensityChoiceSet ? "density-choice-list" : ""}`}
          aria-label="Agent 澄清选项"
        >
          {densityCandidateCount > 0 ? (
            <p className="density-choice-guidance" role="status">
              已找到 {densityCandidateCount} 个真实地图候选；点击一个真实地图候选确认并写入时间轴。
            </p>
          ) : null}
          {options.some((option) => option.action === "continue_clarification") ? (
            <p className="density-choice-guidance" role="status">
              已保存当前澄清进度；选择后会按新约束继续，尚不会写入时间轴。
            </p>
          ) : null}
          {displayOptions.map((option) => {
            if (!option.id) {
              return null;
            }
            const key = `${option.index}-${option.label}`;
            const choiceKey = `${turnId}:${option.id}`;
            const outdatedComparisonChoice = isOutdatedComparisonChoice(option, activeComparisonPreview);
            const lifecycle = outdatedComparisonChoice
              ? "stale"
              : executingChoiceKey === choiceKey
                ? "executing"
                : (choiceLifecycleByKey[choiceKey] ?? option.lifecycle ?? "offered");
            const optionDisabled =
              disabled ||
              ["executing", "consumed", "failed_terminal", "expired", "stale", "cancelled"].includes(lifecycle);
            if (isCustomClarificationOption(option)) {
              const value = customValues[key] ?? "";
              return (
                <div className="clarification-custom-option" key={key}>
                  <span>{option.displayIndex ?? option.index}</span>
                  <label>
                    <MarkdownMessage content={option.label} />
                    <input
                      aria-label="自填选项内容"
                      disabled={optionDisabled}
                      onChange={(event) => setCustomValues((current) => ({ ...current, [key]: event.target.value }))}
                      onKeyDown={(event) => {
                        if (event.key !== "Enter") {
                          return;
                        }
                        event.preventDefault();
                        if (!optionDisabled && value.trim()) {
                          onCustomSelect(option, value);
                        }
                      }}
                      placeholder={customClarificationPlaceholder(option)}
                      value={value}
                    />
                  </label>
                  <button
                    data-choice-id={option.id}
                    data-checkpoint-id={option.checkpointId}
                    data-dimension-id={option.dimensionId}
                    disabled={optionDisabled || !value.trim()}
                    onClick={() => onCustomSelect(option, value)}
                    type="button"
                  >
                    {outdatedComparisonChoice
                      ? "已失效"
                      : lifecycle === "executing"
                        ? option.action === "continue_clarification"
                          ? "正在按新约束继续"
                          : "执行中"
                        : lifecycle === "consumed"
                          ? "已处理"
                          : isDensityChoiceSet && option.action === "manual_continuation"
                            ? "搜索候选"
                            : "发送"}
                  </button>
                </div>
              );
            }
            return (
              <button
                data-choice-action={option.action}
                data-brief-id={option.briefId}
                data-choice-id={option.id}
                data-checkpoint-id={option.checkpointId}
                data-day-number={option.dayNumber}
                data-dimension-id={option.dimensionId}
                data-planning-slot-id={option.planningSlotId}
                data-pool-id={option.poolId}
                data-semantic-value={
                  typeof option.semanticValue === "string"
                    ? option.semanticValue
                    : option.semanticValue
                      ? JSON.stringify(option.semanticValue)
                      : undefined
                }
                disabled={optionDisabled}
                key={key}
                onClick={() => onSelect(option)}
                type="button"
              >
                <span>{option.displayIndex ?? option.index}</span>
                <MarkdownMessage
                  content={
                    lifecycle === "executing"
                      ? `${option.label}${option.action === "continue_clarification" ? "（正在按新约束继续）" : "（执行中）"}`
                      : lifecycle === "consumed"
                        ? `${option.label}（已处理）`
                        : outdatedComparisonChoice
                          ? `${option.label}（已由后续规划请求取代）`
                          : lifecycle === "failed_retryable"
                            ? `${option.label}（可重试）`
                            : option.label
                  }
                />
              </button>
            );
          })}
        </div>
      ) : null}
    </>
  );
}

function isRenderableStructuredChoice(option: ClarificationOption) {
  return Boolean(option.id && option.action);
}

function PlanComparisonActions({
  activeComparisonPreview,
  choiceLifecycleByKey,
  disabled,
  executingChoiceKey,
  onSelect,
  options,
  turnId
}: {
  activeComparisonPreview: PlanComparisonPreviewState;
  choiceLifecycleByKey: Record<string, AgentChoiceOption["lifecycle"]>;
  disabled: boolean;
  executingChoiceKey: string | null;
  onSelect: (option: ClarificationOption) => void;
  options: ClarificationOption[];
  turnId: string;
}) {
  const [expanded, setExpanded] = useState(false);
  const confirmations = options.filter((option) => option.action === "select_plan_proposal");
  const continuations = options.filter((option) => option.action !== "select_plan_proposal");
  const visible = [...(expanded ? confirmations : confirmations.slice(0, 3)), ...continuations];
  return (
    <section aria-label="方案操作" className="plan-comparison-actions">
      <strong>方案操作</strong>
      {confirmations.length > 3 ? (
        <button
          className="clarification-options-expand"
          onClick={() => setExpanded((current) => !current)}
          type="button"
        >
          {expanded ? "收起其他方案" : `展开全部 ${confirmations.length} 个方案`}
        </button>
      ) : null}
      {visible.map((option) => {
        if (!option.id) {
          return null;
        }
        const key = `${turnId}:${option.id}`;
        const outdated = isOutdatedComparisonChoice(option, activeComparisonPreview);
        const lifecycle = outdated
          ? "stale"
          : executingChoiceKey === key
            ? "executing"
            : (choiceLifecycleByKey[key] ?? option.lifecycle ?? "offered");
        const optionDisabled =
          disabled || ["executing", "consumed", "failed_terminal", "expired", "stale", "cancelled"].includes(lifecycle);
        return (
          <button
            data-choice-action={option.action}
            data-choice-id={option.id}
            disabled={optionDisabled}
            key={option.id}
            onClick={() => onSelect(option)}
            type="button"
          >
            <span>{option.displayIndex ?? option.index}</span>
            <MarkdownMessage
              content={
                lifecycle === "executing"
                  ? `${option.label}（执行中）`
                  : lifecycle === "consumed"
                    ? `${option.label}（已处理）`
                    : outdated
                      ? `${option.label}（已由后续规划请求取代）`
                      : lifecycle === "failed_retryable"
                        ? `${option.label}（可重试）`
                        : option.label
              }
            />
          </button>
        );
      })}
    </section>
  );
}

function isCustomClarificationOption(option: ClarificationOption) {
  if (typeof option.allowsManualInput === "boolean") {
    return option.allowsManualInput;
  }
  return option.kind === "custom_input" || /自己填写|自定义|手动输入|具体地点/.test(option.label);
}

function customClarificationPlaceholder(option: ClarificationOption) {
  if (option.action === "continue_clarification") {
    return "补充你的偏好或限制";
  }
  if (/绕路|路线偏好|交通方式/.test(option.label)) {
    return "说明交通方式、步行节奏和可接受的绕路幅度";
  }
  if (/天数|几天|日程天数/.test(option.label)) {
    return "输入天数，如 2 天";
  }
  return "输入高德可检索的具体地点";
}
function MarkdownMessage({ content }: { content: string }) {
  return (
    <ReactMarkdown
      allowedElements={[
        "p",
        "strong",
        "em",
        "ul",
        "ol",
        "li",
        "a",
        "code",
        "pre",
        "blockquote",
        "h1",
        "h2",
        "h3",
        "br",
        "table",
        "thead",
        "tbody",
        "tr",
        "th",
        "td"
      ]}
      components={{
        a: ({ href, children }) => {
          const safeHref = typeof href === "string" && /^https?:\/\//i.test(href) ? href : undefined;
          return safeHref ? (
            <a href={safeHref} rel="noreferrer" target="_blank">
              {children}
            </a>
          ) : (
            <span>{children}</span>
          );
        }
      }}
      remarkPlugins={[remarkGfm]}
      unwrapDisallowed
    >
      {content}
    </ReactMarkdown>
  );
}

function pendingStepFromExecutionEvent(
  event: AgentPlanningEvent,
  current: PendingPlanningStep | null
): PendingPlanningStep | null {
  // Heartbeats only prove that the stream is alive. They must not replace the
  // last user-meaningful planning stage with a generic waiting label.
  if (event.type === "heartbeat" || event.userVisible === false) {
    return current;
  }
  const visibleLabel = userPlanningStage(event);
  const detail =
    event.status === "failed"
      ? "当前阶段未完成，请查看回复中的可操作说明。"
      : event.detail || planningEventStatusLabel(event.status);
  if (event.status === "completed" && current?.label === visibleLabel) {
    return {
      ...current,
      status: "completed",
      detail,
      metadata: event.metadata,
      durationMs: event.durationMs,
      runElapsedMs: eventRunElapsedMs(event)
    };
  }
  const sequence = current && current.label === visibleLabel ? current.sequence : (current?.sequence ?? 0) + 1;
  return {
    type: event.type,
    label: visibleLabel,
    status: event.status,
    detail,
    sequence,
    providerName: event.providerName,
    fallbackUsed: event.fallbackUsed,
    failureReason: event.failureReason,
    metadata: event.metadata,
    timestamp: event.timestamp,
    durationMs: eventDurationMs(event),
    runElapsedMs: eventRunElapsedMs(event)
  };
}

function upsertReasoningStatus(
  statuses: AgentReasoningStatus[],
  incoming: AgentReasoningStatus
): AgentReasoningStatus[] {
  const sameRun = incoming.runId ? statuses.filter((item) => !item.runId || item.runId === incoming.runId) : statuses;
  return aggregateStatuses([...sameRun, incoming]);
}

function closeReasoningStatuses(
  statuses: AgentReasoningStatus[],
  terminal: "failed" | "cancelled",
  sessionId: string,
  elapsedMs: number
): AgentReasoningStatus[] {
  const latest = statuses[statuses.length - 1];
  const now = new Date();
  const sequence = Math.max(0, ...statuses.map((item) => item.latestSequence ?? item.sequence)) + 1;
  const terminalStatus: AgentReasoningStatus = {
    messageType: "reasoning_status",
    id: `${latest?.runId || `reasoning_run_${terminal}_${sessionId}_${now.getTime()}`}:result_assembly`,
    sequence,
    runId: latest?.runId || `reasoning_run_${terminal}_${sessionId}_${now.getTime()}`,
    semanticKey: "result_assembly",
    phase: "result",
    status: terminal,
    summary: terminal === "cancelled" ? "方案整理已停止" : "方案结果未完成",
    sourceEventType: terminal === "cancelled" ? "client_cancel_requested" : "client_stream_failure",
    sessionId,
    turnId: latest?.turnId ?? null,
    rootUserTurnId: latest?.rootUserTurnId ?? null,
    assistantTurnId: latest?.assistantTurnId ?? null,
    startedAt: latest?.startedAt ?? new Date(now.getTime() - Math.max(0, elapsedMs)).toISOString(),
    completedAt: now.toISOString(),
    elapsedMs: Math.max(elapsedMs, latest?.elapsedMs ?? 0),
    firstSequence: sequence,
    latestSequence: sequence,
    timestamp: now.toISOString()
  };
  return aggregateStatuses([...statuses.filter((item) => item.status !== "running"), terminalStatus]);
}

function waitForReasoningPoll(delayMs: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, delayMs));
}

function isRecoverableAgentStreamError(error: unknown): boolean {
  if (error instanceof DOMException && error.name === "AbortError") {
    return false;
  }
  if (error instanceof ApiError) {
    return ["STREAM_MISSING_FINAL_RESPONSE", "STREAM_UNAVAILABLE"].includes(error.code) || error.status === 0;
  }
  return error instanceof TypeError;
}

function userPlanningStage(event: AgentPlanningEvent) {
  const evidence = [event.type, event.label, event.actionLabel, event.providerName, event.metadata?.phase]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
  if (/choice|confirm|selection|needs_confirmation|等待.*确认/.test(evidence)) {
    return "等待用户确认";
  }
  if (/persist|write|patch|version|timeline|时间轴/.test(evidence)) {
    return "生成可见时间轴";
  }
  if (/route|feasib|verify|verifier|quality|校验|路线/.test(evidence)) {
    return "校验候选与路线";
  }
  if (/ground|candidate|search|resolve_poi|amap|候选|检索|地点/.test(evidence)) {
    return "检索真实地点候选";
  }
  if (/draft|structure|day_slot|portfolio|每日|结构/.test(evidence)) {
    return "生成每日行程结构";
  }
  return "理解旅行需求";
}

function pendingPlanningEvents(step: PendingPlanningStep | null, localRunElapsedMs = 0): AgentPlanningEvent[] {
  if (!step) {
    return [];
  }
  const now = new Date().toISOString();
  const runElapsedMs = Math.max(step.runElapsedMs ?? 0, localRunElapsedMs);
  return [
    {
      type: step.type,
      label: step.label,
      status: step.status,
      detail: step.detail,
      providerName: step.providerName,
      fallbackUsed: step.fallbackUsed,
      failureReason: step.failureReason,
      durationMs: step.durationMs,
      metadata: { ...step.metadata, sequence: step.sequence, ...(runElapsedMs ? { runElapsedMs } : {}) },
      timestamp: step.timestamp || now
    }
  ];
}

function planningPanelStatusText(events: AgentPlanningEvent[]) {
  const lastSuccessfulPatch = [...events]
    .reverse()
    .find(
      (event) =>
        event.status === "completed" &&
        /patch_itinerary|时间轴 patch|写入.*时间轴/i.test(`${event.type} ${event.label}`)
    );
  const lastFailureIndex = events.map((event) => event.status).lastIndexOf("failed");
  if (lastSuccessfulPatch && events.lastIndexOf(lastSuccessfulPatch) > lastFailureIndex) {
    return lastFailureIndex >= 0 ? "已恢复" : "已完成";
  }
  if (events.some((event) => event.status === "failed")) {
    return "失败";
  }
  if (events.some((event) => event.fallbackUsed || event.status === "fallback")) {
    return "含降级";
  }
  if (events.some((event) => event.status === "waiting")) {
    return "等待中";
  }
  if (events.some((event) => event.status === "querying")) {
    return "正在执行";
  }
  return "已完成";
}

function planningPanelHeaderText(events: AgentPlanningEvent[]) {
  return planningPanelStatusText(events);
}

function planningPanelElapsedText(events: AgentPlanningEvent[]) {
  const values = events
    .map((event) => eventRunDurationMs(event) ?? eventRunElapsedMs(event))
    .filter((value): value is number => typeof value === "number" && value > 0);
  if (!values.length) {
    return "";
  }
  const elapsedMs = Math.max(...values);
  const hasRunning = events.some((event) => event.status === "querying" || event.status === "waiting");
  return `${hasRunning ? "已用" : "总耗时"} ${formatClockDuration(elapsedMs)}`;
}

function planningEventDurationText(event: AgentPlanningEvent) {
  const runDuration = eventRunDurationMs(event);
  if (runDuration && event.type === "agent_run") {
    return `总耗时 ${formatClockDuration(runDuration)}`;
  }
  const duration = eventDurationMs(event);
  if (duration) {
    return `步骤耗时 ${formatCompactDuration(duration)}`;
  }
  return "";
}

function TurnPlanningEventsPanel({
  assistantTurnId = null,
  events,
  onExportTrace,
  planningRunId = null,
  sessionId = null,
  title = "查看详细执行记录",
  portfolioVisibility = null
}: {
  assistantTurnId?: string | null;
  events: AgentPlanningEvent[];
  onExportTrace?: (sessionId: string, assistantTurnId: string, planningRunId: string) => Promise<"copied" | "downloaded">;
  planningRunId?: string | null;
  sessionId?: string | null;
  title?: string;
  portfolioVisibility?: {
    planningDirectionCount: number;
    visibleComparisonProposalCount: number;
    partialComparisonProposalCount: number;
    verifiedComparisonProposalCount: number;
    adoptionReadyProposalCount: number;
  } | null;
}) {
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");
  const [traceExportState, setTraceExportState] = useState<"idle" | "exporting" | "copied" | "downloaded" | "failed">("idle");
  const [traceExportError, setTraceExportError] = useState<string | null>(null);
  const canExportTrace = Boolean(sessionId && assistantTurnId && planningRunId && onExportTrace);
  if (!events.length && !canExportTrace) {
    return null;
  }
  const elapsedText = planningPanelElapsedText(events);
  const visibleEvents = events.filter(isUserVisiblePlanningEvent);
  async function handleCopyPlanningEvents() {
    try {
      await copyTextToClipboard(formatActionTraceForCopy(visibleEvents, title));
      setCopyState("copied");
      window.setTimeout(() => setCopyState("idle"), 1600);
    } catch {
      setCopyState("failed");
      window.setTimeout(() => setCopyState("idle"), 2200);
    }
  }
  async function handleExportTrace() {
    if (!sessionId || !assistantTurnId || !planningRunId || !onExportTrace) {
      return;
    }
    setTraceExportError(null);
    setTraceExportState("exporting");
    try {
      const delivery = await onExportTrace(sessionId, assistantTurnId, planningRunId);
      setTraceExportState(delivery);
    } catch (error) {
      setTraceExportError(friendlyAgentError(error instanceof Error ? error.message : "导出失败，请稍后重试。"));
      setTraceExportState("failed");
      window.setTimeout(() => setTraceExportState("idle"), 3200);
    }
  }
  return (
    <details className="turn-planning-events" aria-label="Agent turn planning process">
      <summary>
        <span>{title}</span>
        <div className="turn-planning-events-actions">
          {elapsedText ? <small className="planning-duration-chip">{elapsedText}</small> : null}
          <em>
            {copyState === "failed" ? "复制失败" : copyState === "copied" ? "已复制" : planningPanelHeaderText(events)}
          </em>
        </div>
      </summary>
      <div className="turn-planning-events-body">
        <header>
          <button aria-label={`复制${title}操作过程`} type="button" onClick={() => void handleCopyPlanningEvents()}>
            复制操作
          </button>
          {canExportTrace ? (
            <button
              aria-label="导出完整规划 Trace"
              disabled={traceExportState === "exporting"}
              type="button"
              onClick={() => void handleExportTrace()}
            >
              {traceExportState === "exporting" ? "正在获取…" : "复制 Trace"}
            </button>
          ) : null}
        </header>
        {canExportTrace ? (
          <div className="planning-trace-export">
            <small>复制本轮完整脱敏 Trace；过大或无法复制时下载文件。</small>
            {traceExportState === "copied" || traceExportState === "downloaded" ? (
              <small role="status">{traceExportState === "copied" ? "已复制 Trace" : "已下载完整 Trace 文件"}</small>
            ) : null}
            {traceExportState === "failed" ? (
              <small role="alert">{traceExportError ?? "导出失败，请稍后重试。"}</small>
            ) : null}
          </div>
        ) : null}
        {portfolioVisibility ? (
          <div className="planning-outcome-statuses" aria-label="Portfolio visibility">
            <span>规划方向：{portfolioVisibility.planningDirectionCount}</span>
            <span>已展示方案：{portfolioVisibility.visibleComparisonProposalCount}</span>
            <span>其中部分方案：{portfolioVisibility.partialComparisonProposalCount}</span>
            <span>严格验证通过：{portfolioVisibility.verifiedComparisonProposalCount}</span>
            <span>可采用方案：{portfolioVisibility.adoptionReadyProposalCount}</span>
          </div>
        ) : null}
        <ol aria-label="Agent 可见动作轨迹">
          {visibleEvents.map((event, index) => {
            const detailMessage = event.detail ? friendlyProviderMessage(event.detail) : "";
            const failureMessage = event.failureReason ? friendlyProviderMessage(event.failureReason) : "";
            const sequence = eventSequenceNumber(event, index);
            return (
              <li
                className={`turn-planning-event ${event.status}`}
                key={`${event.type}-${event.label}-${index}`}
                value={sequence}
              >
                <strong>{event.actionLabel || event.label}</strong>
                <span>
                  {planningEventStatusLabel(event.status)}
                  {event.fallbackUsed ? " · 已降级" : ""}
                  {planningEventDurationText(event) ? ` · ${planningEventDurationText(event)}` : ""}
                </span>
                {event.goal ? <p>目标：{event.goal}</p> : null}
                {event.inputSummary ? <p>输入：{event.inputSummary}</p> : null}
                {event.resultSummary || detailMessage ? <p>结果：{event.resultSummary || detailMessage}</p> : null}
                {event.decisionSummary ? <p>决定：{event.decisionSummary}</p> : null}
                {event.effectSummary ? <p>影响：{event.effectSummary}</p> : null}
                {failureMessage && failureMessage !== detailMessage ? <small>{failureMessage}</small> : null}
                <ToolEventResultDetails event={event} />
              </li>
            );
          })}
        </ol>
      </div>
    </details>
  );
}

function isUserVisiblePlanningEvent(event: AgentPlanningEvent) {
  if (typeof event.userVisible === "boolean") {
    return event.userVisible;
  }
  return !["plan", "memory", "normalize_request", "generate_day_slots", "decompose_intent_pools", "agent_run"].includes(
    event.type
  );
}

function formatActionTraceForCopy(events: AgentPlanningEvent[], title: string) {
  const lines = [`${title}操作过程（${events.length} 步）`];
  for (const [index, event] of events.entries()) {
    lines.push(
      "",
      `${event.sequence || index + 1}. ${event.actionLabel || event.label}`,
      `状态：${planningEventStatusLabel(event.status)}`
    );
    if (event.goal) lines.push(`目标：${event.goal}`);
    if (event.inputSummary) lines.push(`输入：${event.inputSummary}`);
    if (event.resultSummary || event.detail) lines.push(`结果：${event.resultSummary || event.detail}`);
    if (event.decisionSummary) lines.push(`决定：${event.decisionSummary}`);
    if (event.effectSummary) lines.push(`影响：${event.effectSummary}`);
  }
  return lines.join("\n");
}

function hasConversationCopyContent(
  turns: ConversationTurn[],
  pendingAgentMessage: string,
  pendingPlanningStep: PendingPlanningStep | null
) {
  return turns.length > 0 || Boolean(pendingAgentMessage.trim()) || Boolean(pendingPlanningStep);
}

function readStoredActiveAgentSessionId() {
  try {
    return typeof window === "undefined" ? "" : (window.localStorage?.getItem(ACTIVE_AGENT_SESSION_STORAGE_KEY) ?? "");
  } catch {
    return "";
  }
}

function readStoredPanelWidth(key: string, fallback: number, min: number, max: number) {
  try {
    const stored = typeof window === "undefined" ? null : window.localStorage?.getItem(key);
    const value = stored === null || stored === "" ? NaN : Number(stored);
    return Number.isFinite(value) ? Math.min(Math.max(value, min), max) : fallback;
  } catch {
    return fallback;
  }
}

function persistPanelWidth(key: string, width: number) {
  try {
    if (typeof window !== "undefined") {
      window.localStorage?.setItem(key, String(width));
    }
  } catch {
    // Layout remains usable when browser storage is unavailable.
  }
}

function rememberActiveAgentSession(sessionId: string | null) {
  try {
    if (typeof window === "undefined") {
      return;
    }
    if (sessionId) {
      window.localStorage?.setItem(ACTIVE_AGENT_SESSION_STORAGE_KEY, sessionId);
      return;
    }
    window.localStorage?.removeItem(ACTIVE_AGENT_SESSION_STORAGE_KEY);
  } catch {
    // Workspace recovery must not depend on localStorage availability.
  }
}

function formatConversationForCopy({
  session,
  turns,
  pendingAgentMessage,
  pendingPlanningStep,
  agentRunElapsedMs
}: {
  session: AgentSession | null;
  turns: ConversationTurn[];
  pendingAgentMessage: string;
  pendingPlanningStep: PendingPlanningStep | null;
  agentRunElapsedMs: number;
}) {
  const lines = [`对话：${session?.title ?? "当前对话"}`, `城市：${session?.city ?? "未设置"}`];
  for (const turn of [...turns].sort((left, right) => left.turnIndex - right.turnIndex)) {
    lines.push("", formatTurnHeader(turn), sanitizeUserCopyText(turn.content) || "(空消息)");
    if (turn.failureReason) {
      lines.push(`失败原因：${friendlyProviderMessage(turn.failureReason)}`);
    }
  }
  if (pendingAgentMessage.trim()) {
    lines.push("", "用户（发送中）", sanitizeUserCopyText(pendingAgentMessage));
  }
  if (pendingPlanningStep) {
    lines.push("", `当前步骤：${pendingPlanningStep.label}`);
  }
  return lines.join("\n");
}

function sanitizeUserCopyText(value: string) {
  const internalPattern =
    /(riskSearchDiagnostics|webSearchProviderDiagnostics|processId|toolSchemaHashes|server_generic_day_slot_fallback_reason)/i;
  return value
    .split(/\r?\n/)
    .filter((line) => !internalPattern.test(line))
    .join("\n")
    .trim();
}

function formatConversationDebugForCopy(args: Parameters<typeof formatConversationForCopy>[0]) {
  const lines = [
    `对话：${args.session?.title ?? "当前对话"}`,
    `城市：${args.session?.city ?? "未设置"}`,
    `会话ID：${args.session?.sessionId ?? "未创建"}`,
    `Active Version：${args.session?.activeVersionId ?? "无"}`
  ];
  for (const turn of [...args.turns].sort((left, right) => left.turnIndex - right.turnIndex)) {
    lines.push("", formatTurnHeader(turn), turn.content.trim() || "(空消息)");
    const events = turn.role === "assistant" ? mergedTurnPlanningEvents(turn.planningSteps, turn.toolEvents) : [];
    if (events.length) {
      lines.push("", indentCopiedBlock(formatPlanningEventsForCopy(events, `Turn ${turn.turnIndex} 规划过程`)));
    }
  }
  if (args.pendingAgentMessage.trim()) {
    lines.push("", "用户（发送中）", args.pendingAgentMessage.trim());
  }
  if (args.pendingPlanningStep) {
    lines.push(
      "",
      indentCopiedBlock(
        formatPlanningEventsForCopy(pendingPlanningEvents(args.pendingPlanningStep, args.agentRunElapsedMs), "当前步骤")
      )
    );
  }
  return lines.join("\n");
}

function formatTurnHeader(turn: ConversationTurn) {
  const role = turn.role === "assistant" ? "AI" : turn.role === "user" ? "用户" : "系统";
  const status = turn.status === "active" ? "有效" : turn.status === "failed" ? "失败" : "已被取代";
  const createdAt = formatCopyTimestamp(turn.createdAt);
  const version = turn.itineraryVersionId ? ` · version=${turn.itineraryVersionId}` : "";
  return `${role} #${turn.turnIndex} · ${status} · ${createdAt}${version}`;
}

function formatCopyTimestamp(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { hour12: false });
}

function indentCopiedBlock(value: string) {
  return value
    .split("\n")
    .map((line) => `  ${line}`)
    .join("\n");
}

function eventSequenceNumber(event: AgentPlanningEvent, fallbackIndex: number) {
  const sequence = event.metadata?.sequence;
  return typeof sequence === "number" && Number.isFinite(sequence) && sequence > 0
    ? Math.trunc(sequence)
    : fallbackIndex + 1;
}

function eventDurationMs(event: AgentPlanningEvent) {
  return numericDuration(event.durationMs);
}

function eventRunElapsedMs(event: AgentPlanningEvent) {
  return numericMetadataDuration(event.metadata, "runElapsedMs");
}

function eventRunDurationMs(event: AgentPlanningEvent) {
  return (
    numericMetadataDuration(event.metadata, "runDurationMs") ??
    (event.type === "agent_run" ? eventDurationMs(event) : undefined)
  );
}

function numericMetadataDuration(metadata: Record<string, unknown> | undefined, key: string) {
  if (!metadata) {
    return undefined;
  }
  return numericDuration(metadata[key]);
}

function numericDuration(value: unknown) {
  if (typeof value === "number" && Number.isFinite(value) && value > 0) {
    return Math.trunc(value);
  }
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value);
    if (Number.isFinite(parsed) && parsed > 0) {
      return Math.trunc(parsed);
    }
  }
  return undefined;
}

function formatClockDuration(durationMs: number) {
  const totalSeconds = Math.max(0, Math.floor(durationMs / 1000));
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

function formatCompactDuration(durationMs: number) {
  if (durationMs < 1000) {
    return `${durationMs}ms`;
  }
  if (durationMs < 60_000) {
    return `${(durationMs / 1000).toFixed(durationMs < 10_000 ? 1 : 0)}s`;
  }
  return formatClockDuration(durationMs);
}

async function copyTextToClipboard(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "true");
  textarea.style.position = "fixed";
  textarea.style.left = "-9999px";
  document.body.appendChild(textarea);
  textarea.select();
  const copied = document.execCommand("copy");
  document.body.removeChild(textarea);
  if (!copied) {
    throw new Error("Clipboard copy failed");
  }
}

const DIAGNOSTIC_RESULT_PREVIEW_KEYS = [
  "poolReports",
  "unresolvedSlots",
  "dayReadiness",
  "persistableSegmentCount",
  "routeAnchorRequiredCount",
  "routeAnchorSelectedCount",
  "unresolvedDays",
  "minimumViableRule",
  "versionCreated",
  "blockingStage",
  "dayReadinessSummary",
  "planningPreview",
  "nextActions"
];

function diagnosticMetadataPreview(metadata: Record<string, unknown>): Record<string, unknown> | undefined {
  return DIAGNOSTIC_RESULT_PREVIEW_KEYS.some((key) => Object.prototype.hasOwnProperty.call(metadata, key))
    ? metadata
    : undefined;
}

function hasPreviewContent(value: unknown): boolean {
  if (value === null || value === undefined) {
    return false;
  }
  if (Array.isArray(value)) {
    return value.length > 0;
  }
  if (typeof value === "object") {
    return Object.keys(value as Record<string, unknown>).length > 0;
  }
  return true;
}

function toolEventResultPreview(metadata: Record<string, unknown>): unknown {
  for (const preview of [metadata.resultPreview, metadata.outputPreview, metadata.toolResult]) {
    if (hasPreviewContent(preview)) {
      return preview;
    }
  }
  return diagnosticMetadataPreview(metadata);
}

function formatPlanningEventsForCopy(events: AgentPlanningEvent[], title: string): string {
  const lines = [`${title}（${events.length} 步）`, `整体状态：${planningPanelStatusText(events)}`];
  const elapsedText = planningPanelElapsedText(events);
  if (elapsedText) {
    lines.push(`执行耗时：${elapsedText}`);
  }
  events.forEach((event, index) => {
    const metadata = event.metadata ?? {};
    const inputPreview = metadata.inputPreview;
    const resultPreview = toolEventResultPreview(metadata);
    const durationText = planningEventDurationText(event);
    lines.push(
      "",
      `${eventSequenceNumber(event, index)}. ${event.label}`,
      `状态：${planningEventStatusLabel(event.status)}${event.fallbackUsed ? " · 已降级" : ""}`,
      `类型：${event.type}`
    );
    if (durationText) {
      lines.push(`耗时：${durationText}`);
    }
    if (event.timestamp) {
      lines.push(`时间：${event.timestamp}`);
    }
    if (event.providerName) {
      lines.push(`Provider：${friendlyProviderMessage(event.providerName)}`);
    }
    if (event.detail) {
      lines.push(`详情：${friendlyProviderMessage(event.detail)}`);
    }
    if (event.failureReason) {
      lines.push(`失败原因：${friendlyProviderMessage(event.failureReason)}`);
    }
    if (inputPreview) {
      lines.push("工具输入：", formatToolPreview(inputPreview));
    }
    if (resultPreview) {
      lines.push("返回结果：", formatToolPreview(resultPreview));
    }
  });
  return lines.join("\n");
}

function ToolEventResultDetails({ event }: { event: AgentPlanningEvent }) {
  const metadata = event.metadata ?? {};
  const resultPreview = toolEventResultPreview(metadata);
  const inputPreview = metadata.inputPreview;
  if (!resultPreview && !inputPreview && !event.providerName && !event.failureReason) {
    return null;
  }
  const summary = event.status === "failed" ? "查看失败详情" : "查看工具详情";
  return (
    <details className="turn-tool-result">
      <summary>{summary}</summary>
      <div className="turn-tool-result-grid">
        {event.providerName ? (
          <div>
            <b>Provider</b>
            <span>{friendlyProviderMessage(event.providerName)}</span>
          </div>
        ) : null}
        {event.failureReason ? (
          <div>
            <b>失败原因</b>
            <span>{friendlyProviderMessage(event.failureReason)}</span>
          </div>
        ) : null}
      </div>
      {inputPreview ? (
        <>
          <b className="turn-tool-result-heading">工具输入</b>
          <pre>{formatToolPreview(inputPreview)}</pre>
        </>
      ) : null}
      {resultPreview ? (
        <>
          <b className="turn-tool-result-heading">返回结果</b>
          <pre>{formatToolPreview(resultPreview)}</pre>
        </>
      ) : null}
    </details>
  );
}

function mergedTurnPlanningEvents(
  planningSteps: AgentPlanningEvent[] | undefined,
  toolEvents: AgentPlanningEvent[] | undefined
): AgentPlanningEvent[] {
  const events: AgentPlanningEvent[] = [];
  const seen = new Set<string>();
  for (const event of [...(planningSteps ?? []), ...(toolEvents ?? [])]) {
    const key = [event.type, event.label, event.status, event.timestamp, event.detail, event.failureReason ?? ""].join(
      "|"
    );
    if (seen.has(key)) {
      continue;
    }
    seen.add(key);
    events.push(event);
  }
  return events;
}

const RESUMABLE_AGENT_FAILURE_MARKERS = [
  "tool_loop_failed",
  "tool_loop_no_itinerary_write",
  "staged_initial_pipeline_failed",
  "provider_rate_limited",
  "waiting_for_poi_grounding",
  "semantic_candidate_hint_missing",
  "地图服务限流",
  "地图服务暂时限流",
  "可续跑草案"
];

function canResumeAgentTurn(turn: ConversationTurn, activeVersionId?: string | null): boolean {
  if (turn.role !== "assistant") {
    return false;
  }
  if (turn.status === "failed") {
    const haystack = turn.failureReason ?? "";
    return RESUMABLE_AGENT_FAILURE_MARKERS.some((marker) => haystack.includes(marker));
  }
  if (activeVersionId) {
    return false;
  }
  const haystack = [
    turn.failureReason ?? "",
    turn.content,
    ...(turn.planningSteps ?? []).map((event) =>
      [event.type, event.label, event.detail, event.failureReason ?? "", JSON.stringify(event.metadata ?? {})].join(" ")
    ),
    ...(turn.toolEvents ?? []).map((event) =>
      [event.type, event.label, event.detail, event.failureReason ?? "", JSON.stringify(event.metadata ?? {})].join(" ")
    )
  ].join(" ");
  if (turn.status !== "active" || turn.itineraryVersionId) {
    return false;
  }
  return RESUMABLE_AGENT_FAILURE_MARKERS.some((marker) => haystack.includes(marker));
}

function isMapProviderRateLimitedText(value: string) {
  return value.includes("provider_rate_limited") || /地图服务.{0,8}限流/.test(value);
}

function formatToolPreview(value: unknown) {
  if (typeof value === "string") {
    return friendlyProviderMessage(value);
  }
  try {
    return JSON.stringify(sanitizeToolPreview(value), null, 2);
  } catch {
    return String(value);
  }
}

function sanitizeToolPreview(value: unknown): unknown {
  if (Array.isArray(value)) {
    return value.map((item) => sanitizeToolPreview(item));
  }
  if (!value || typeof value !== "object") {
    return value;
  }
  return Object.fromEntries(
    Object.entries(value as Record<string, unknown>)
      .filter(([key]) => !hiddenToolPreviewKey(key))
      .map(([key, item]) => [key, sanitizeToolPreview(item)])
  );
}

function hiddenToolPreviewKey(key: string) {
  return [
    "providername",
    "failurereason",
    "fallbackused",
    "apikey",
    "api_key",
    "authorization",
    "token",
    "password",
    "secret"
  ].includes(key.toLowerCase());
}

function upsertAgentSessionSummary(
  sessions: ReturnType<typeof plannerStore.getSnapshot>["agentSessions"] | undefined,
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
  return [summary, ...(sessions ?? []).filter((item) => item.sessionId !== session.sessionId)];
}

function upsertConversationTurn(
  turns: ReturnType<typeof plannerStore.getSnapshot>["conversationTurns"],
  turn: ReturnType<typeof plannerStore.getSnapshot>["conversationTurns"][number]
) {
  const withoutExisting = turns.filter((item) => item.id !== turn.id);
  return [...withoutExisting, turn].sort((left, right) => left.turnIndex - right.turnIndex);
}

function replaceEditedConversationTurn(
  turns: ReturnType<typeof plannerStore.getSnapshot>["conversationTurns"],
  requestedTurnId: string,
  editedTurn: ReturnType<typeof plannerStore.getSnapshot>["conversationTurns"][number],
  supersededTurnIds: string[]
) {
  const supersededIds = new Set(supersededTurnIds);
  supersededIds.add(requestedTurnId);
  const withoutEditedTurn = turns
    .filter((turn) => turn.id !== editedTurn.id)
    .map((turn) => (supersededIds.has(turn.id) ? { ...turn, status: "superseded" as const } : turn));
  return [...withoutEditedTurn, editedTurn].sort((left, right) => left.turnIndex - right.turnIndex);
}

function isCurrentAgentRequest(agentRequestSeqRef: { current: number }, requestSeq: number, sessionId: string) {
  const currentSessionId = plannerStore.getSnapshot().agentSession?.sessionId;
  return agentRequestSeqRef.current === requestSeq && currentSessionId === sessionId;
}

function isCurrentSessionRequest(sessionRequestSeqRef: { current: number }, requestSeq: number) {
  return sessionRequestSeqRef.current === requestSeq;
}

function planningEventStatusLabel(status: string) {
  const labels: Record<string, string> = {
    waiting: "等待中",
    querying: "查询中",
    completed: "已完成",
    succeeded: "已完成",
    fallback: "已降级",
    failed: "失败"
  };
  return labels[status] ?? status;
}

function friendlyProviderMessage(message: string) {
  if (!message) {
    return "";
  }
  if (
    /KEY|TOKEN|SECRET|API|PROVIDER|provider|mock|fallback|AMAP|WEB_SERVICE|configured|not configured|bocha/i.test(
      message
    )
  ) {
    return "相关服务暂时不可用，当前结果可能不完整；请稍后重新查询或检查服务配置。";
  }
  return message.replace(/\bfallback\b/gi, "降级");
}

function findFirstOperationSegmentId(operations: Array<Record<string, unknown>>) {
  for (const operation of operations) {
    if (typeof operation.segmentId === "string" && operation.segmentId) {
      return operation.segmentId;
    }
    if (typeof operation.segment_id === "string" && operation.segment_id) {
      return operation.segment_id;
    }
  }
  return null;
}

function buildVisiblePlanningProcess({
  userMessage,
  snapshot,
  preferenceCard,
  city,
  finalResult,
  completed = false,
  pendingPoi = false,
  comparisonFallback = false
}: {
  userMessage: string;
  snapshot: ReturnType<typeof plannerStore.getSnapshot>;
  preferenceCard: ReturnType<typeof defaultPreferenceCard>;
  city: string;
  finalResult: string;
  completed?: boolean;
  pendingPoi?: boolean;
  comparisonFallback?: boolean;
}): AgentPlanningProcessModel {
  const plan = snapshot.itineraryPlan;
  const effectivePreference =
    effectivePreferenceMemoryText(snapshot.preferenceMemory?.memoryText ?? "") ||
    effectivePreferenceMemoryText(preferenceCard.summaryText);
  const routeOptions = plan?.routeOptions ?? [];
  const weatherSignals = plan?.weatherSignals ?? [];
  const trafficCrowdingSignals = plan?.trafficCrowdingSignals ?? [];
  const ticketLookupResults = plan?.ticketLookupResults ?? [];
  const weatherFallback = weatherSignals.some((signal) => signal.fallbackUsed || signal.dataStatus === "fallback");
  const ticketFallback = ticketLookupResults.some((ticket) => ticket.fallbackUsed);
  const days = plan?.days.length ?? 0;
  const constraints: AgentPlanningProcessModel["constraints"] = [];
  if (city) {
    constraints.push({ label: "当前城市上下文", value: city });
  }
  if (days) {
    constraints.push({ label: "天数", value: `${days} 天` });
  }
  if (preferenceCard.budgetRange) {
    constraints.push({ label: "预算", value: preferenceCard.budgetRange });
  }
  if (snapshot.selectedRouteOptionId) {
    constraints.push({ label: "路线", value: "沿用当前选中路线" });
  }
  if (ticketLookupResults.length) {
    constraints.push({ label: "开放/预约", value: `${ticketLookupResults.length} 条查询结果` });
  }
  if (weatherSignals.length) {
    constraints.push({ label: "天气", value: weatherSignals[0].dailySummary });
  }
  if (trafficCrowdingSignals.length) {
    constraints.push({ label: "拥挤程度", value: trafficCrowdingSignals[0].crowdingLevel });
  }
  const now = new Date().toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
  return {
    userRequirement: userMessage,
    preferenceSummary: effectivePreference || "还没有记录明确旅行偏好，Agent 会在后续对话中逐步整理。",
    constraints,
    steps: [
      {
        id: "parse-input",
        label: "解析用户输入",
        status: completed ? "completed" : "querying",
        source: "Agent message parser",
        updatedAt: now,
        confidence: 0.9
      },
      {
        id: "extract-preferences",
        label: "提取旅行偏好",
        status: completed ? "completed" : "querying",
        source: "Preference extractor",
        updatedAt: now,
        confidence: effectivePreference ? 0.86 : 0.45,
        fallbackNote: effectivePreference ? undefined : "未识别到明确偏好，本次不会把默认模板传给 Agent。"
      },
      {
        id: "resolve-poi",
        label: "查询/解析 POI",
        status: pendingPoi ? "waiting" : completed ? "completed" : "querying",
        source: "AMap POI WebService",
        updatedAt: now,
        confidence: pendingPoi ? 0.55 : 0.88,
        fallbackNote: pendingPoi ? "存在多候选或低置信 POI，需要用户确认后才会写入最终行程。" : undefined
      },
      {
        id: "generate-draft",
        label: "生成 itinerary draft",
        status: completed ? "completed" : "waiting",
        source: "Itinerary patch validator + SQLite snapshot",
        updatedAt: now,
        confidence: plan ? 0.92 : 0.6
      },
      {
        id: "route-traffic",
        label: "查询路线/交通",
        status: routeOptions.length ? "completed" : completed ? "fallback" : "waiting",
        source: routeOptions[0]?.source || "AMap route provider",
        updatedAt: now,
        confidence: routeOptions.length ? 0.84 : 0.5,
        fallbackNote: routeOptions.length ? undefined : "路线数据暂不可用时，仍保留可编辑行程草稿。"
      },
      {
        id: "amap-weather",
        label: "查询高德天气",
        status: weatherSignals.length
          ? weatherFallback
            ? "fallback"
            : "completed"
          : completed
            ? "fallback"
            : "waiting",
        source: weatherSignals[0]?.providerName || weatherSignals[0]?.source || "AMap Weather provider",
        updatedAt: now,
        confidence: weatherSignals[0]?.confidence ?? 0.45,
        fallbackNote: weatherFallback
          ? friendlyProviderMessage(
              weatherSignals[0]?.userVisibleCaveat ||
                weatherSignals[0]?.failureReason ||
                "天气服务暂时不可用，当前结果可能不完整。"
            )
          : undefined
      },
      {
        id: "ticket-reservation",
        label: "联网搜索景点开放/预约",
        status: ticketLookupResults.length
          ? ticketFallback
            ? "fallback"
            : "completed"
          : completed
            ? "fallback"
            : "waiting",
        source: ticketLookupResults[0]?.providerName || "Ticket lookup provider",
        updatedAt: now,
        confidence: ticketLookupResults.length ? 0.78 : 0.45,
        fallbackNote: ticketFallback
          ? friendlyProviderMessage(
              ticketLookupResults[0]?.providerFailureReason || "公开来源查询暂时不可用，当前票务/预约结果可能不完整。"
            )
          : ticketLookupResults.length
            ? undefined
            : "票务/预约结果缺失时显示待查询，不阻塞行程查看。"
      },
      {
        id: "compare-plans",
        label: "生成多个对比方案",
        status: comparisonFallback
          ? "fallback"
          : snapshot.planComparison
            ? "completed"
            : completed
              ? "waiting"
              : "waiting",
        source: "Plan comparison service",
        updatedAt: now,
        confidence: snapshot.planComparison ? 0.82 : 0.4,
        fallbackNote: comparisonFallback ? "多个方案比较失败，但主行程仍可查看和编辑。" : undefined
      }
    ],
    resultSummary: finalResult
  };
}

function dayNumberForAppSegment(plan: ItineraryPlan | null, segmentId: string | null) {
  if (!plan || !segmentId) {
    return null;
  }
  return plan.days.find((day) => day.segments.some((segment) => segment.id === segmentId))?.dayNumber ?? null;
}

function segmentExistsInPlan(plan: ItineraryPlan | null, segmentId: string | null) {
  return Boolean(
    plan && segmentId && plan.days.some((day) => day.segments.some((segment) => segment.id === segmentId))
  );
}

function friendlyAgentError(message: string) {
  if (/no_supported_trip_date_expression/i.test(message)) {
    return "本轮没有解析到新的出行日期；如果这是对已有方案的补充，请从原方案继续，系统会恢复原日期。";
  }
  if (/spatial_focus|simple_open_spatial_preference_unresolved/i.test(message)) {
    return "还需确认活动区域；确认前不会搜索地点或写入正式行程。";
  }
  if (
    /portfolio_partial_anchor_grounding_evidence_missing|portfolio_partial_grounding_projection_mismatch/i.test(message)
  ) {
    return "当前地点证据与待生成时间轴不一致，本轮尚未写入时间轴。请刷新该待补时段的候选后重试。";
  }
  if (/User message has no itinerary version to restore/i.test(message)) {
    return "这条消息尚未生成可恢复行程，将从最近可用状态重新生成。";
  }
  if (/CUQPS_HAS_EXCEEDED_THE_LIMIT|高德 POI 查询频率超限/i.test(message)) {
    return "高德 POI 查询频率超限，请稍后重试。行程未写入。";
  }
  if (
    /Agent returned invalid structured itinerary JSON|Agent output was not valid JSON|AgentStructuredOutput|Pydantic|ValidationError|validation errors? for|fullItinerary|poiResolutionRequests|Field required|model_validate/i.test(
      message
    )
  ) {
    return "Agent 返回的结构化行程格式不符合要求，已拒绝更新行程。";
  }
  return message;
}

import {
  AgentSession,
  AgentSessionSummary,
  ConversationTurn,
  ItineraryPlan,
  MapPoi,
  PendingPoiCandidate,
  PlanComparisonResponse,
  PlanningRun,
  PreferenceMemory,
  PreferenceSummaryCard
} from "../services/apiClient";
import type { ItineraryAgentContext } from "../components/timeline/itineraryWorkspace";
import { createComparisonPreviewState, type PlanComparisonPreviewState } from "./planComparisonPreview";

export type PlannerPanel = "agent" | "map" | "timeline";

export type DensityMapComparisonAnchor = MapPoi & {
  startTime?: string | null;
  timeWindow?: string | null;
};

export type DensityMapCandidateChoice = {
  amapId: string;
  sourceAssistantTurnId: string;
  choiceId: string;
  label?: string;
};

export type DensityMapComparisonContext = {
  sessionId: string;
  sourceAssistantTurnId: string;
  candidateRecordId: string;
  briefId: string;
  poolId: string;
  dayNumber: number;
  planningSlotId: string;
  timeWindow: string;
  displayNeed: string;
  anchors: DensityMapComparisonAnchor[];
  candidateChoices?: DensityMapCandidateChoice[];
};

export type PlannerState = {
  selectedCity: string;
  activePanel: PlannerPanel;
  providerMode: "default" | "mock";
  agentSession: AgentSession | null;
  agentSessions: AgentSessionSummary[];
  conversationTurns: ConversationTurn[];
  activeVersionId: string | null;
  itineraryPlan: ItineraryPlan | null;
  planComparison: PlanComparisonResponse | null;
  comparisonPreview: PlanComparisonPreviewState;
  preferenceCard: PreferenceSummaryCard | null;
  preferenceMemory: PreferenceMemory | null;
  pendingPoiCandidates: PendingPoiCandidate[];
  activeDensityMapComparison: DensityMapComparisonContext | null;
  poiSelectionStatuses: Record<string, "pending" | "addedToDay" | "ignored">;
  supersededTurnIds: string[];
  lastPatchError: string;
  candidateMapPois: MapPoi[];
  selectedMapPoi: MapPoi | null;
  selectedDayNumber: number;
  selectedSegmentId: string | null;
  timelineSelectionRequestId: number;
  selectedRouteOptionId: string | null;
  previewRouteOptionId: string | null;
  routeWarnings: string[];
  itineraryAgentContext: ItineraryAgentContext | null;
  timelineCopyText: string;
  lastPlanningRun: PlanningRun | null;
};

type Listener = (state: PlannerState) => void;

const initialState: PlannerState = {
  selectedCity: "北京",
  activePanel: "agent",
  providerMode: "mock",
  agentSession: null,
  agentSessions: [],
  conversationTurns: [],
  activeVersionId: null,
  itineraryPlan: null,
  planComparison: null,
  comparisonPreview: createComparisonPreviewState(),
  preferenceCard: null,
  preferenceMemory: null,
  pendingPoiCandidates: [],
  activeDensityMapComparison: null,
  poiSelectionStatuses: {},
  supersededTurnIds: [],
  lastPatchError: "",
  candidateMapPois: [],
  selectedMapPoi: null,
  selectedDayNumber: 1,
  selectedSegmentId: null,
  timelineSelectionRequestId: 0,
  selectedRouteOptionId: null,
  previewRouteOptionId: null,
  routeWarnings: [],
  itineraryAgentContext: null,
  timelineCopyText: "",
  lastPlanningRun: null
};

let state = initialState;
const listeners = new Set<Listener>();

export const plannerStore = {
  getSnapshot: () => state,
  subscribe(listener: Listener) {
    listeners.add(listener);
    return () => {
      listeners.delete(listener);
    };
  },
  setState(next: Partial<PlannerState>) {
    state = { ...state, ...next };
    listeners.forEach((listener) => listener(state));
  }
};

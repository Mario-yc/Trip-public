import { afterEach, expect, test } from "vitest";
import { buildAgentContext, buildPlanningContext } from "../../src/state/agentContext";
import { plannerStore } from "../../src/state/plannerStore";
import { ItineraryPlan, MapPoi, PreferenceSummaryCard } from "../../src/services/apiClient";
import {
  createComparisonPreviewState,
  markComparisonPlanAdopted,
  upsertVisibleComparisonPlan
} from "../../src/state/planComparisonPreview";

afterEach(() => {
  plannerStore.setState({
    selectedCity: "北京",
    activeVersionId: null,
    itineraryPlan: null,
    preferenceCard: null,
    preferenceMemory: null,
    pendingPoiCandidates: [],
    candidateMapPois: [],
    selectedMapPoi: null,
    selectedDayNumber: 1,
    selectedSegmentId: null,
    selectedRouteOptionId: null,
    itineraryAgentContext: null,
    timelineCopyText: "",
    comparisonPreview: createComparisonPreviewState()
  });
});

test("planning context reflects the timeline and preferences without exposing transient map selection", () => {
  const selectedPoi = mapPoi("B000PALACE", "故宫博物院");
  plannerStore.setState({
    selectedCity: "北京",
    activeVersionId: "ver_1",
    itineraryPlan: planFixture(selectedPoi),
    selectedSegmentId: "seg_1",
    selectedDayNumber: 1,
    selectedRouteOptionId: "route_1",
    selectedMapPoi: selectedPoi,
    candidateMapPois: [selectedPoi],
    pendingPoiCandidates: [
      {
        id: "cand_1",
        query: "故宫",
        city: "北京",
        category: "scenic",
        status: "pending",
        candidates: [selectedPoi],
        createdAt: "2026-06-18T00:00:00Z"
      }
    ],
    preferenceMemory: {
      userId: "default",
      memoryText: "# 我的旅行偏好\n\n## 旅行节奏\n- 暂无明确记录。\n- 喜欢轻松不赶路。\n",
      autoUpdateEnabled: true,
      createdAt: "2026-06-18T00:00:00Z",
      updatedAt: "2026-06-18T00:00:00Z"
    },
    preferenceCard: preferenceCard("## 旅行节奏\n- 暂无明确记录。\n")
  });

  const context = buildPlanningContext(plannerStore.getSnapshot());

  expect(context.activeVersionId).toBe("ver_1");
  expect(context.selectedSegment?.id).toBe("seg_1");
  expect(context).not.toHaveProperty("selectedMapPoi");
  expect(context).not.toHaveProperty("pendingPoiCandidateId");
  expect(context.memoryText).toBe("旅行节奏：喜欢轻松不赶路。");
  expect(context.preferenceMemory?.memoryText).toBe("旅行节奏：喜欢轻松不赶路。");
  expect(context.preferenceCard).toEqual({
    id: "card_1",
    profileId: "profile_1",
    summaryText: "旅行节奏：喜欢轻松不赶路。",
    status: "draft"
  });
  expect(JSON.stringify(context)).not.toContain("自由行");
  expect(context.candidateMapPois).toEqual([
    {
      id: "B000PALACE",
      name: "故宫博物院",
      type: "风景名胜",
      address: "景山前街4号",
      longitude: 116.397026,
      latitude: 39.918058
    }
  ]);
});

test("agent context embeds the planning context and user message", () => {
  const selectedPoi = mapPoi("B000PALACE", "故宫博物院");
  plannerStore.setState({
    selectedCity: "北京",
    activeVersionId: "ver_2",
    itineraryPlan: planFixture(selectedPoi),
    selectedSegmentId: "seg_1",
    selectedMapPoi: selectedPoi,
    candidateMapPois: [selectedPoi],
    preferenceMemory: {
      userId: "default",
      memoryText: "# 我的旅行偏好\n\n## 交通偏好\n- 公共交通优先。\n",
      autoUpdateEnabled: false,
      createdAt: "2026-06-18T00:00:00Z",
      updatedAt: "2026-06-18T00:00:00Z"
    }
  });

  const context = buildAgentContext(plannerStore.getSnapshot(), preferenceCard("## 交通偏好\n- 打车优先。\n"), "把故宫放到下午");

  expect(context.currentUserMessage).toBe("把故宫放到下午");
  expect(context.activeVersionId).toBe("ver_2");
  expect(context.memoryText).toBe("交通偏好：公共交通优先。");
  const effectiveCard = context.preferenceCard;
  expect(effectiveCard).not.toBeNull();
  expect(effectiveCard?.summaryText).toBe("交通偏好：公共交通优先。");
  expect(JSON.stringify(effectiveCard)).not.toContain("travelerTypes");
  expect(JSON.stringify(effectiveCard)).not.toContain("partySize");
  expect(context.timelineContext.activeVersionId).toBe("ver_2");
  expect(context.timelineContext.selectedSegment?.poi.name).toBe("故宫博物院");
  expect(context.candidatePoiIds).toEqual(["B000PALACE"]);
});

test("agent context omits default preference card when no actual preference exists", () => {
  plannerStore.setState({
    selectedCity: "北京",
    preferenceMemory: {
      userId: "default",
      memoryText: "# 我的旅行偏好\n\n## 旅行节奏\n- 暂无明确记录。\n",
      autoUpdateEnabled: true,
      createdAt: "2026-06-18T00:00:00Z",
      updatedAt: "2026-06-18T00:00:00Z"
    }
  });

  const context = buildAgentContext(plannerStore.getSnapshot(), preferenceCard(""), "帮我安排北京一天");

  expect(context.memoryText).toBe("");
  expect(context.currentPreferenceSummary).toBe("");
  expect(context.preferenceCard).toBeNull();
  expect(context.preferenceMemory).toBeNull();
  expect(context.preferenceCardId).toBeNull();
  expect(JSON.stringify(context)).not.toContain("暂无明确记录");
});

test("an empty overview reports its real view while leaving direction identity empty", () => {
  const context = buildAgentContext(
    plannerStore.getSnapshot(),
    preferenceCard(""),
    "北京高校两日游，公共交通优先",
    { activeView: "overview" }
  );

  expect(context.currentUserMessage).toBe("北京高校两日游，公共交通优先");
  expect(context.viewContext).toEqual({
    schemaVersion: "agent-view-context-v1",
    activeView: "overview",
    editingProposal: null,
    focusedProposal: null
  });
});

test("an overview with an active itinerary but no confirmed direction identity stays fail-closed", () => {
  plannerStore.setState({
    activeVersionId: "ver_orphan_active",
    itineraryPlan: planFixture(mapPoi("B000PALACE", "故宫博物院")),
    comparisonPreview: createComparisonPreviewState()
  });

  const context = buildAgentContext(
    plannerStore.getSnapshot(),
    preferenceCard(""),
    "补充第一天",
    { activeView: "overview" }
  );

  expect(context.viewContext).toEqual({
    schemaVersion: "agent-view-context-v1",
    activeView: "overview",
    editingProposal: null,
    focusedProposal: null
  });
});

test("agent context keeps view semantics separate from the unchanged explicit user intent", () => {
  const proposal = {
    planningSelectionRootTurnId: "root_direction",
    rootPortfolioId: "portfolio_direction",
    proposalId: "direction_a",
    sourceAssistantTurnId: "assistant_direction_a",
    choiceId: "confirm_direction_a",
    materialFingerprint: "f".repeat(64),
    repairChoiceId: "simple_direction_repair_a",
    workflowMode: "simple_direction_v1" as const,
    status: "complete",
    isPartial: false,
    isAdopted: false,
    adoptionReady: true,
    activeVersionId: "ver_direction_a",
    expectedBaseVersionId: "ver_direction_a",
    title: "高校集中方向",
    days: [],
    pendingSlots: [],
    routeEvidence: [],
    budgetSummary: "中等预算",
    routeSummary: "路线已核验",
    nextAction: "confirm_edit" as const,
    nextActionLabel: "确认编辑",
    tradeoffSummary: "",
    colorKey: "ocean"
  };
  const comparisonPreview = markComparisonPlanAdopted(
    upsertVisibleComparisonPlan(createComparisonPreviewState(), proposal).state,
    proposal.proposalId,
    "ver_direction_a"
  );
  plannerStore.setState({
    activeVersionId: "ver_direction_a",
    comparisonPreview
  });

  const context = buildAgentContext(
    plannerStore.getSnapshot(),
    preferenceCard(""),
    "修改当前方案的第一天",
    { activeView: "comparison" }
  );

  expect(context.currentUserMessage).toBe("修改当前方案的第一天");
  expect(context.viewContext).toEqual({
    schemaVersion: "agent-view-context-v1",
    activeView: "comparison",
    editingProposal: {
      planningSelectionRootTurnId: "root_direction",
      rootPortfolioId: "portfolio_direction",
      proposalId: "direction_a",
      sourceAssistantTurnId: "assistant_direction_a",
      activeVersionId: "ver_direction_a"
    },
    focusedProposal: {
      planningSelectionRootTurnId: "root_direction",
      rootPortfolioId: "portfolio_direction",
      proposalId: "direction_a",
      sourceAssistantTurnId: "assistant_direction_a",
      materialFingerprint: "f".repeat(64),
      repairChoiceId: "simple_direction_repair_a"
    }
  });
});

function preferenceCard(summaryText: string): PreferenceSummaryCard {
  return {
    id: "card_1",
    profileId: "profile_1",
    partySize: 1,
    travelerTypes: ["自由行"],
    budgetRange: "",
    pacePreference: "",
    summaryText,
    items: [],
    status: "draft"
  };
}

function mapPoi(id: string, name: string): MapPoi {
  return {
    id,
    amapId: id,
    name,
    type: "风景名胜",
    city: "北京",
    district: "东城区",
    address: "景山前街4号",
    longitude: 116.397026,
    latitude: 39.918058,
    category: "scenic",
    source: "amap-place-search",
    sourceNote: "高德 WebService POI 搜索",
    confidence: 0.91,
    photos: []
  };
}

function planFixture(poi: MapPoi): ItineraryPlan {
  const plannerPoi = { ...poi, sourceUrl: poi.sourceUrl ?? undefined };
  return {
    id: "plan_1",
    title: "北京 1 日游",
    city: "北京",
    templateType: "agent_mvp",
    budgetEstimate: 0,
    budgetDeltaExplanation: "",
    decisionRationale: "",
    status: "draft",
    days: [
      {
        id: "day_1",
        dayNumber: 1,
        weatherSummary: "",
        riskSummary: "",
        totalEstimatedCost: 0,
        segments: [
          {
            id: "seg_1",
            startTime: "09:30",
            endTime: "11:30",
            kind: "activity",
            poi: plannerPoi,
            transportMode: "walk",
            estimatedCost: 0,
            notes: ""
          }
        ]
      }
    ],
    routeOptions: [
      {
        id: "route_1",
        fromSegmentId: "seg_1",
        toSegmentId: "seg_1",
        fromPoiId: poi.id,
        toPoiId: poi.id,
        provider: "amap-webservice",
        mode: "walking",
        label: "步行",
        isSelected: true,
        sortOrder: 1,
        transportMode: "walk",
        distanceMeters: 0,
        durationSeconds: 0,
        durationMinutes: 0,
        costAmount: 0,
        costCurrency: "CNY",
        costEstimate: 0,
        crowdingRisk: "low",
        source: "amap-webservice",
        polyline: [],
        steps: [],
        providerPayload: {},
        error: null,
        queriedAt: "2026-06-18T00:00:00Z"
      }
    ],
    weatherSignals: [],
    trafficCrowdingSignals: [],
    ticketLookupResults: []
  };
}

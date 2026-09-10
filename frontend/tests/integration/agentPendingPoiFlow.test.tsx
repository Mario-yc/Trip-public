import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { AppShell } from "../../src/components/AppShell";
import { plannerStore } from "../../src/state/plannerStore";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  plannerStore.setState({
    agentSession: null,
    conversationTurns: [],
    activeVersionId: null,
    itineraryPlan: null,
    pendingPoiCandidates: [],
    selectedMapPoi: null,
    selectedDayNumber: 1,
    selectedSegmentId: null
  });
});

test("pending POI evidence is preserved without opening a global selection state", async () => {
  const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) {
      return jsonResponse({ mode: "mock", default: [], mock: [] });
    }
    if (path.endsWith("/map/config")) {
      return jsonResponse({ provider: "amap", enabled: true, jsApiKey: "web-key" });
    }
    if (path.endsWith("/preferences/extract")) {
      return jsonResponse({
        summaryCard: {
          id: "card_pending",
          profileId: "pref_pending",
          partySize: 1,
          travelerTypes: ["adult"],
          budgetRange: "3000 左右",
          pacePreference: "轻松不赶路",
          summaryText: "用户偏好轻松不赶路的行程，预算约 3000 左右，希望确认胡同餐厅候选。",
          items: [],
          status: "draft",
          providerName: "preference-rule-extractor",
          fallbackUsed: false
        }
      });
    }
    if (path.endsWith("/agent/sessions")) {
      return jsonResponse(agentSessionWithSkeleton());
    }
    if (path.endsWith("/agent/sessions/sess_pending/messages")) {
      return jsonResponse(agentPendingResponse());
    }
    if (path.endsWith("/agent/sessions/sess_pending")) {
      return jsonResponse(agentSessionAfterConfirm());
    }
    if (path.endsWith("/itineraries/plan_pending/patch") && init?.method === "POST") {
      const body = JSON.parse(String(init.body ?? "{}"));
      expect(body.operations[0]).toMatchObject({
        op: "add_segment",
        dayId: "day_pending_1",
        title: "老胡同餐厅"
      });
      expect(body.operations[0].amapPoi.id).toBe("B000FOOD");
      expect(body.preferenceSummary).toBe("用户偏好轻松不赶路的行程，预算约 3000 左右，希望确认胡同餐厅候选。");
      expect(body.baseVersionId).toBeNull();
      expect(body.planningContext).toMatchObject({
        city: "北京",
        currentPreferenceSummary: "用户偏好轻松不赶路的行程，预算约 3000 左右，希望确认胡同餐厅候选。",
        selectedDayNumber: 1,
        selectedMapPoi: { id: "B000FOOD" },
        patchIntent: "map_add_selected_poi",
        pendingPoiCandidateId: "cand_food"
      });
      return jsonResponse({
        itinerary: itineraryWithConfirmedPoi(),
        patch: { id: "patch_pending", validationStatus: "accepted" },
        version: { id: "ver_pending_1", versionNumber: 1, sourceType: "manual" },
        validationErrors: [],
        pendingPoiCandidates: [remainingPendingCandidate()]
      });
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);

  fireEvent.change(screen.getByLabelText("Agent 对话文本"), {
    target: { value: "北京一天，想找一个老胡同餐厅" }
  });
  fireEvent.click(screen.getByText("发送给 Agent"));

  await waitFor(() => expect(screen.queryByLabelText("Agent planning process")).toBeNull());
  expect(screen.queryByText("Agent 规划过程")).toBeNull();
  expect(screen.queryByText(/思维链|chain of thought|内部推理/)).toBeNull();

  await waitFor(() => expect(plannerStore.getSnapshot().pendingPoiCandidates.map((candidate) => candidate.id)).toEqual(["cand_food"]));
  expect(screen.queryByText(/正在选择 POI/)).toBeNull();
  expect(screen.queryByText(/你可以在地图上点击标点加入某天行程/)).toBeNull();
  expect(screen.queryByText("关闭 POI 选择状态")).toBeNull();
  expect(screen.queryByText("忽略该 POI")).toBeNull();
  expect(plannerStore.getSnapshot().agentSession?.pendingPoiCandidates.map((candidate) => candidate.id)).toEqual(["cand_food"]);
  expect(plannerStore.getSnapshot().activeVersionId).toBeNull();
  expect(fetchMock.mock.calls.some((call) => String(call[0]).endsWith("/itineraries/plan_pending/patch"))).toBe(false);
});

test("legacy global pending POI actions are not rendered or dispatched", async () => {
  const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) {
      return jsonResponse({ mode: "mock", default: [], mock: [] });
    }
    if (path.endsWith("/map/config")) {
      return jsonResponse({ provider: "amap", enabled: true, jsApiKey: "web-key" });
    }
    if (path.endsWith("/preferences/extract")) {
      return jsonResponse({
        summaryCard: {
          id: "card_pending",
          profileId: "pref_pending",
          partySize: 1,
          travelerTypes: ["adult"],
          budgetRange: "3000 左右",
          pacePreference: "轻松不赶路",
          summaryText: "用户偏好轻松不赶路的行程，预算约 3000 左右，希望确认胡同餐厅候选。",
          items: [],
          status: "draft",
          providerName: "preference-rule-extractor",
          fallbackUsed: false
        }
      });
    }
    if (path.endsWith("/agent/sessions")) {
      return jsonResponse(agentSessionWithSkeleton());
    }
    if (path.endsWith("/agent/sessions/sess_pending/messages")) {
      return jsonResponse(agentPendingResponse());
    }
    if (path.endsWith("/agent/sessions/sess_pending/pending-poi-candidates/cand_food/reject") && init?.method === "POST") {
      return jsonResponse(agentSessionAfterReject());
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);

  fireEvent.change(screen.getByLabelText("Agent 对话文本"), {
    target: { value: "北京一天，想找一个老胡同餐厅" }
  });
  fireEvent.click(screen.getByText("发送给 Agent"));

  await waitFor(() => expect(plannerStore.getSnapshot().pendingPoiCandidates.map((candidate) => candidate.id)).toEqual(["cand_food"]));
  expect(screen.queryByText("忽略该 POI")).toBeNull();
  expect(screen.queryByText(/正在选择 POI/)).toBeNull();
  expect(fetchMock.mock.calls.some((call) => String(call[0]).endsWith("/agent/sessions/sess_pending/pending-poi-candidates/cand_food/reject"))).toBe(false);
  expect(plannerStore.getSnapshot().agentSession?.pendingPoiCandidates.map((candidate) => candidate.id)).toEqual(["cand_food"]);
});

test("reload restores active version turns and pending candidates from current server session", async () => {
  vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) {
      return jsonResponse({ mode: "mock", default: [], mock: [] });
    }
    if (path.endsWith("/map/config")) {
      return jsonResponse({ provider: "amap", enabled: true, jsApiKey: "web-key" });
    }
    if (path.endsWith("/agent/sessions")) {
      return jsonResponse({ sessions: [] });
    }
    if (path.endsWith("/agent/sessions/current")) {
      return jsonResponse(agentSessionForReload());
    }
    return jsonResponse({}, 404);
  }));

  render(<AppShell />);

  await waitFor(() => expect(plannerStore.getSnapshot().activeVersionId).toBe("ver_reload"));
  expect(plannerStore.getSnapshot().conversationTurns.map((turn) => turn.id)).toEqual(["turn_reload_user", "turn_reload_assistant"]);
  expect(plannerStore.getSnapshot().pendingPoiCandidates.map((candidate) => candidate.id)).toEqual(["cand_reload"]);
  expect(plannerStore.getSnapshot().agentSession?.pendingPoiCandidates.map((candidate) => candidate.id)).toEqual(["cand_reload"]);
});

test("a raw map POI is not sent back as Agent write authority", async () => {
  const messageBodies: AgentMessageRequestBody[] = [];
  const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) {
      return jsonResponse({ mode: "mock", default: [], mock: [] });
    }
    if (path.endsWith("/map/config")) {
      return jsonResponse({ provider: "amap", enabled: true, jsApiKey: "web-key" });
    }
    if (path.endsWith("/preferences/extract")) {
      return jsonResponse({
        summaryCard: {
          id: "card_pending",
          profileId: "pref_pending",
          partySize: 1,
          travelerTypes: ["adult"],
          budgetRange: "3000 左右",
          pacePreference: "轻松不赶路",
          summaryText: "用户偏好轻松不赶路的行程，预算约 3000 左右，希望确认胡同餐厅候选。",
          items: [],
          status: "draft",
          providerName: "preference-rule-extractor",
          fallbackUsed: false
        }
      });
    }
    if (path.endsWith("/agent/sessions")) {
      return jsonResponse(agentSessionWithSkeleton());
    }
    if (path.endsWith("/agent/sessions/sess_pending/messages")) {
      messageBodies.push(JSON.parse(String(init?.body ?? "{}")));
      return jsonResponse(messageBodies.length === 1 ? agentPendingResponse() : agentConfirmationResponse());
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);

  fireEvent.change(screen.getByLabelText("Agent 对话文本"), {
    target: { value: "北京一天，想找一个老胡同餐厅" }
  });
  fireEvent.click(screen.getByText("发送给 Agent"));

  await waitFor(() => expect(plannerStore.getSnapshot().pendingPoiCandidates.map((candidate) => candidate.id)).toEqual(["cand_food"]));
  plannerStore.setState({ selectedMapPoi: amapPoi() });

  fireEvent.change(screen.getByLabelText("Agent 对话文本"), {
    target: { value: "就用这个" }
  });
  fireEvent.click(screen.getByText("发送给 Agent"));

  await waitFor(() => expect(messageBodies).toHaveLength(2));
  const confirmationBody = messageBodies[1];
  if (!confirmationBody) {
    throw new Error("Expected second Agent message request body.");
  }
  expect(confirmationBody.context).not.toHaveProperty("pendingPoiCandidateId");
  expect(confirmationBody.context.timelineContext ?? {}).not.toHaveProperty("pendingPoiCandidateId");
  expect(confirmationBody.context).not.toHaveProperty("selectedMapPoi");
  expect(confirmationBody.context).not.toHaveProperty("selectedMapPoiId");
});

test("stale selected POI does not imply pending candidate confirmation", async () => {
  const messageBodies: AgentMessageRequestBody[] = [];
  const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) {
      return jsonResponse({ mode: "mock", default: [], mock: [] });
    }
    if (path.endsWith("/map/config")) {
      return jsonResponse({ provider: "amap", enabled: true, jsApiKey: "web-key" });
    }
    if (path.endsWith("/preferences/extract")) {
      return jsonResponse({
        summaryCard: {
          id: "card_pending",
          profileId: "pref_pending",
          partySize: 1,
          travelerTypes: ["adult"],
          budgetRange: "3000 左右",
          pacePreference: "轻松不赶路",
          summaryText: "用户偏好轻松不赶路的行程，预算约 3000 左右，希望确认胡同餐厅候选。",
          items: [],
          status: "draft",
          providerName: "preference-rule-extractor",
          fallbackUsed: false
        }
      });
    }
    if (path.endsWith("/agent/sessions")) {
      return jsonResponse(agentSessionWithSkeleton());
    }
    if (path.endsWith("/agent/sessions/sess_pending/messages")) {
      messageBodies.push(JSON.parse(String(init?.body ?? "{}")));
      return jsonResponse(messageBodies.length === 1 ? agentPendingResponse() : agentConfirmationResponse());
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);

  fireEvent.change(screen.getByLabelText("Agent 对话文本"), {
    target: { value: "北京一天，想找一个老胡同餐厅" }
  });
  fireEvent.click(screen.getByText("发送给 Agent"));

  await waitFor(() => expect(plannerStore.getSnapshot().pendingPoiCandidates.map((candidate) => candidate.id)).toEqual(["cand_food"]));
  plannerStore.setState({ selectedMapPoi: amapPoi() });

  fireEvent.change(screen.getByLabelText("Agent 对话文本"), {
    target: { value: "就用这个" }
  });
  fireEvent.click(screen.getByText("发送给 Agent"));

  await waitFor(() => expect(messageBodies).toHaveLength(2));
  const confirmationBody = messageBodies[1];
  if (!confirmationBody) {
    throw new Error("Expected second Agent message request body.");
  }
  expect(confirmationBody.context).not.toHaveProperty("pendingPoiCandidateId");
  expect(confirmationBody.context.timelineContext ?? {}).not.toHaveProperty("pendingPoiCandidateId");
  expect(confirmationBody.context).not.toHaveProperty("selectedMapPoi");
  expect(confirmationBody.context).not.toHaveProperty("selectedMapPoiId");
});

test("explicit university POIs from an agent message are shown without unrelated default attractions", async () => {
  vi.stubGlobal("AMap", {
    Map: vi.fn(function Map() {
      return { destroy: vi.fn(), on: vi.fn(), setZoomAndCenter: vi.fn(), lngLatToContainer: vi.fn(() => ({ x: 320, y: 240 })) };
    }),
    Marker: vi.fn(function Marker() {
      return { setMap: vi.fn(), on: vi.fn() };
    }),
    Polyline: vi.fn(function Polyline() {
      return { setMap: vi.fn(), setOptions: vi.fn() };
    })
  });
  vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) {
      return jsonResponse({ mode: "mock", default: [], mock: [] });
    }
    if (path.endsWith("/map/config")) {
      return jsonResponse({ provider: "amap", enabled: true, jsApiKey: "web-key" });
    }
    if (path.endsWith("/preferences/extract")) {
      return jsonResponse({
        summaryCard: {
          id: "card_university",
          profileId: "pref_university",
          partySize: 1,
          travelerTypes: ["adult"],
          budgetRange: "",
          pacePreference: "轻松不赶路",
          summaryText: "用户明确希望上午去北京大学，下午去清华大学。",
          items: [],
          status: "draft",
          providerName: "preference-rule-extractor",
          fallbackUsed: false
        }
      });
    }
    if (path.endsWith("/agent/sessions")) {
      return jsonResponse({ ...agentSessionWithSkeleton(), sessionId: "sess_university" });
    }
    if (path.endsWith("/agent/sessions/sess_university/messages")) {
      return jsonResponse(agentUniversityResponse());
    }
    return jsonResponse({}, 404);
  }));

  render(<AppShell />);

  fireEvent.change(screen.getByLabelText("Agent 对话文本"), {
    target: { value: "上午去北京大学，下午去清华大学" }
  });
  fireEvent.click(screen.getByText("发送给 Agent"));

  await waitFor(() => {
    expect(screen.getAllByText("北京大学").length).toBeGreaterThan(0);
    expect(screen.getAllByText("清华大学").length).toBeGreaterThan(0);
  });
  expect(screen.queryByText("故宫博物院")).toBeNull();
  expect(screen.queryByText("天坛公园")).toBeNull();
  const snapshot = plannerStore.getSnapshot();
  expect(snapshot.activeVersionId).toBe("ver_university");
  expect(snapshot.agentSession?.activeVersionId).toBe("ver_university");
  expect(snapshot.pendingPoiCandidates).toEqual([]);
  expect(snapshot.agentSession?.pendingPoiCandidates).toEqual([]);
  expect(snapshot.itineraryPlan?.days[0].segments.map((segment) => segment.poi.name)).toEqual(["北京大学", "清华大学"]);
});

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" }
  });
}

type AgentMessageRequestBody = {
  content: string;
  context: {
    pendingPoiCandidateId?: string | null;
    timelineContext?: {
      pendingPoiCandidateId?: string | null;
    };
    selectedMapPoi?: {
      id?: string;
    };
  };
};

function agentSessionWithSkeleton() {
  return {
    sessionId: "sess_pending",
    status: "active",
    city: "北京",
    title: "北京 AI 行程",
    activePlanId: "plan_pending",
    activeVersionId: null,
    turns: [],
    itinerary: itinerarySkeleton(),
    pendingPoiCandidates: []
  };
}

function agentSessionAfterConfirm() {
  return {
    ...agentSessionWithSkeleton(),
    activeVersionId: "ver_pending_1",
    itinerary: itineraryWithConfirmedPoi(),
    pendingPoiCandidates: []
  };
}

function agentSessionAfterReject() {
  return {
    ...agentSessionWithSkeleton(),
    itinerary: itinerarySkeleton(),
    pendingPoiCandidates: []
  };
}

function agentSessionForReload() {
  const createdAt = "2026-06-11T00:03:00Z";
  return {
    ...agentSessionWithSkeleton(),
    sessionId: "sess_reload",
    activeVersionId: "ver_reload",
    turns: [
      {
        id: "turn_reload_user",
        role: "user",
        content: "北京一天",
        turnIndex: 1,
        status: "active",
        createdAt,
        updatedAt: createdAt
      },
      {
        id: "turn_reload_assistant",
        role: "assistant",
        content: "还有一个 POI 候选需要确认。",
        turnIndex: 2,
        status: "active",
        createdAt,
        updatedAt: createdAt
      }
    ],
    itinerary: itinerarySkeleton(),
    pendingPoiCandidates: [
      {
        id: "cand_reload",
        query: "胡同咖啡",
        city: "北京",
        category: "food",
        status: "pending",
        candidates: [{ ...amapPoi(), id: "B000CAFE", name: "胡同咖啡" }],
        createdAt
      }
    ]
  };
}

function agentPendingResponse() {
  const createdAt = "2026-06-11T00:00:00Z";
  return {
    userTurn: {
      id: "turn_pending_user",
      role: "user",
      content: "北京一天，想找一个老胡同餐厅",
      turnIndex: 1,
      status: "active",
      createdAt,
      updatedAt: createdAt
    },
    assistantTurn: {
      id: "turn_pending_assistant",
      role: "assistant",
      content: "找到多个胡同餐厅候选，请先确认。",
      turnIndex: 2,
      status: "active",
      createdAt,
      updatedAt: createdAt
    },
    itinerary: null,
    version: null,
    pendingPoiCandidates: [
      {
        id: "cand_food",
        query: "老胡同餐厅",
        city: "北京",
        category: "food",
        status: "pending",
        candidates: [amapPoi()],
        createdAt
      }
    ],
    warnings: []
  };
}

function remainingPendingCandidate() {
  return {
    id: "cand_remaining",
    query: "胡同咖啡",
    city: "北京",
    category: "food",
    status: "pending",
    candidates: [{ ...amapPoi(), id: "B000CAFE", name: "胡同咖啡" }],
    createdAt: "2026-06-11T00:02:00Z"
  };
}

function agentUniversityResponse() {
  const createdAt = "2026-06-11T00:00:00Z";
  return {
    userTurn: {
      id: "turn_university_user",
      role: "user",
      content: "上午去北京大学，下午去清华大学",
      turnIndex: 1,
      status: "active",
      createdAt,
      updatedAt: createdAt
    },
    assistantTurn: {
      id: "turn_university_assistant",
      role: "assistant",
      content: "已按你的要求只安排北京大学和清华大学。",
      turnIndex: 2,
      status: "active",
      itineraryVersionId: "ver_university",
      createdAt,
      updatedAt: createdAt
    },
    itinerary: universityItinerary(),
    version: { id: "ver_university", versionNumber: 1, sourceType: "agent" },
    pendingPoiCandidates: [],
    warnings: []
  };
}

function agentConfirmationResponse() {
  const createdAt = "2026-06-11T00:01:00Z";
  return {
    userTurn: {
      id: "turn_confirm_user",
      role: "user",
      content: "就用这个",
      turnIndex: 3,
      status: "active",
      createdAt,
      updatedAt: createdAt
    },
    assistantTurn: {
      id: "turn_confirm_assistant",
      role: "assistant",
      content: "已收到你确认的候选 POI。",
      turnIndex: 4,
      status: "active",
      createdAt,
      updatedAt: createdAt
    },
    itinerary: null,
    version: null,
    pendingPoiCandidates: [
      {
        id: "cand_food",
        query: "老胡同餐厅",
        city: "北京",
        category: "food",
        status: "pending",
        candidates: [amapPoi()],
        createdAt
      }
    ],
    warnings: []
  };
}

function itinerarySkeleton() {
  return {
    id: "plan_pending",
    title: "北京 AI 行程",
    city: "北京",
    templateType: "agent_mvp",
    budgetEstimate: 0,
    budgetDeltaExplanation: "等待用户确认 POI。",
    decisionRationale: "Agent session skeleton。",
    status: "draft",
    days: [
      {
        id: "day_pending_1",
        dayNumber: 1,
        title: "Day 1 待规划",
        weatherSummary: "等待 Agent 查询天气。",
        riskSummary: "等待 Agent 查询拥挤风险。",
        totalEstimatedCost: 0,
        segments: []
      }
    ],
    routeOptions: [],
    weatherSignals: [],
    trafficCrowdingSignals: [],
    ticketLookupResults: []
  };
}

function itineraryWithConfirmedPoi() {
  return {
    ...itinerarySkeleton(),
    days: [
      {
        ...itinerarySkeleton().days[0],
        title: "Day 1",
        segments: [
          {
            id: "seg_pending_food",
            startTime: "09:30",
            endTime: "10:30",
            kind: "activity",
            poi: {
              id: "poi_pending_food",
              amapId: "B000FOOD",
              name: "老胡同餐厅",
              city: "北京市",
              category: "food",
              latitude: 39.92,
              longitude: 116.41,
              source: "amap-place-search",
              type: "餐饮服务",
              district: "东城区",
              address: "东城区胡同 12 号",
              sourceNote: "高德 WebService POI 搜索",
              confidence: 0.92
            },
            transportMode: "walk",
            estimatedCost: 0,
            notes: "胡同晚餐"
          }
        ]
      }
    ]
  };
}

function universityItinerary() {
  return {
    id: "plan_pending",
    title: "北京大学与清华大学一日行程",
    city: "北京",
    templateType: "agent_mvp",
    budgetEstimate: 0,
    budgetDeltaExplanation: "Agent 估算。",
    decisionRationale: "按用户明确提到的 POI 生成。",
    status: "draft",
    days: [
      {
        id: "day_university",
        dayNumber: 1,
        title: "高校校园一日",
        weatherSummary: "",
        riskSummary: "",
        totalEstimatedCost: 0,
        segments: [
          {
            id: "seg_pku",
            startTime: "09:00",
            endTime: "11:00",
            kind: "activity",
            poi: universityPoi("B000PKU", "北京大学", 116.31088, 39.99281),
            transportMode: "walk",
            estimatedCost: 0,
            notes: "上午游览北京大学。"
          },
          {
            id: "seg_tsinghua",
            startTime: "14:00",
            endTime: "16:00",
            kind: "activity",
            poi: universityPoi("B000THU", "清华大学", 116.32672, 40.00342),
            transportMode: "walk",
            estimatedCost: 0,
            notes: "下午游览清华大学。"
          }
        ]
      }
    ],
    routeOptions: [],
    weatherSignals: [],
    trafficCrowdingSignals: [],
    ticketLookupResults: []
  };
}

function universityPoi(amapId: string, name: string, longitude: number, latitude: number) {
  return {
    id: `poi_${amapId}`,
    amapId,
    name,
    city: "北京市",
    category: "education",
    latitude,
    longitude,
    source: "amap-place-search",
    type: "科教文化服务;学校;高等院校",
    district: "海淀区",
    address: "海淀区",
    sourceNote: "高德 WebService POI 搜索",
    confidence: 0.95
  };
}

function amapPoi() {
  return {
    id: "B000FOOD",
    name: "老胡同餐厅",
    type: "餐饮服务",
    city: "北京市",
    district: "东城区",
    address: "东城区胡同 12 号",
    longitude: 116.41,
    latitude: 39.92,
    category: "food",
    source: "amap-place-search",
    sourceNote: "高德 WebService POI 搜索",
    confidence: 0.62,
    photos: [{ title: "老胡同餐厅", url: "https://example.com/food.jpg" }]
  };
}

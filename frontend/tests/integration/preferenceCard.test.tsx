import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { AppShell } from "../../src/components/AppShell";
import { plannerStore } from "../../src/state/plannerStore";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  window.localStorage.clear();
  plannerStore.setState({
    selectedCity: "北京",
    agentSession: null,
    agentSessions: [],
    conversationTurns: [],
    activeVersionId: null,
    itineraryPlan: null,
    pendingPoiCandidates: [],
    preferenceMemory: null,
    preferenceCard: undefined
  });
});

test("startup restores the last selected session with persisted preference and risk prompts", async () => {
  window.localStorage.setItem("trip.activeAgentSessionId", "sess_with_risk");
  const fetchMock = vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) {
      return jsonResponse({ mode: "mock", default: [], mock: [] });
    }
    if (path.endsWith("/agent/sessions")) {
      return jsonResponse({
        sessions: [
          sessionSummary("sess_latest_empty", "无风险新会话", "2026-06-11T11:00:00Z"),
          sessionSummary("sess_with_risk", "有风险提示会话", "2026-06-11T10:00:00Z")
        ]
      });
    }
    if (path.endsWith("/agent/sessions/sess_with_risk")) {
      return jsonResponse(agentSession(
        preferenceMemory("# 我的旅行偏好\n\n## 旅行节奏\n- 喜欢慢节奏。\n"),
        itineraryPlanWithRisk(),
        "sess_with_risk"
      ));
    }
    if (path.endsWith("/agent/sessions/current")) {
      return jsonResponse(agentSession(preferenceMemory(""), null, "sess_latest_empty"));
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);

  await waitFor(() => expect(screen.getByText("北京风险行程")).toBeTruthy());
  fireEvent.doubleClick(screen.getByRole("button", { name: "旅行偏好卡片" }));
  await waitFor(() => expect(screen.getByText("喜欢慢节奏。")).toBeTruthy());
  fireEvent.click(screen.getByRole("button", { name: "展开风险" }));
  expect(screen.getByText("景点风险搜索")).toBeTruthy();
  const riskArea = screen.getByLabelText("POI risk search alerts");
  fireEvent.click(within(riskArea).getAllByText("故宫博物院")[0]);
  expect(screen.getByText("故宫节假日预约紧张，建议提前确认官方预约。")).toBeTruthy();
  expect(window.localStorage.getItem("trip.activeAgentSessionId")).toBe("sess_with_risk");
  expect(fetchMock.mock.calls.some((call) => String(call[0]).endsWith("/agent/sessions/sess_with_risk"))).toBe(true);
});

test("preference note is editable, saved, auto-updates from agent text, and travels with agent context", async () => {
  const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) {
      return jsonResponse({ mode: "mock", default: [], mock: [] });
    }
    if (path.endsWith("/agent/sessions/current")) {
      return jsonResponse(agentSession(preferenceMemory("# 我的旅行偏好\n\n## 旅行节奏\n- 喜欢轻松不赶路。\n\n## 交通偏好\n- 少换乘。\n\n## 需要确认\n- 暂无明确记录。\n")));
    }
    if (path.endsWith("/preferences/extract")) {
      return jsonResponse({
        summaryCard: {
          id: "card_1",
          profileId: "pref_1",
          partySize: 3,
          travelerTypes: ["adult", "elder"],
          budgetRange: "3000 左右",
          pacePreference: "轻松不赶路",
          summaryText: "用户偏好轻松不赶路的行程，预算约 3000 左右，交通上倾向少换乘，拍照优先。",
          items: [{ label: "少换乘", sourceText: "少换乘" }],
          status: "draft"
        }
      });
    }
    if (path.includes("/preferences/memory") && (!init || init.method === undefined || init.method === "GET")) {
      return jsonResponse(preferenceMemory("# 我的旅行偏好\n\n## 旅行节奏\n- 喜欢轻松不赶路。\n\n## 交通偏好\n- 少换乘。\n\n## 需要确认\n- 暂无明确记录。\n"));
    }
    if (path.includes("/preferences/memory") && init?.method === "PATCH") {
      const body = JSON.parse(String(init?.body ?? "{}"));
      return jsonResponse(preferenceMemory(body.memoryText, body.autoUpdateEnabled));
    }
    if (path.includes("/preferences/memory/restore-default")) {
      return jsonResponse(preferenceMemory("# 我的旅行偏好\n\n## 旅行节奏\n- 暂无明确记录。\n\n## 需要确认\n- 暂无明确记录。\n"));
    }
    if (path.endsWith("/preferences/card_1")) {
      const body = JSON.parse(String(init?.body ?? "{}"));
      return jsonResponse({
        summaryCard: {
          id: "card_1",
          profileId: "pref_1",
          partySize: 3,
          travelerTypes: ["adult", "elder"],
          budgetRange: "3000 左右",
          pacePreference: "轻松不赶路",
          summaryText: body.summaryText,
          items: [{ label: "少换乘", sourceText: "少换乘" }],
          status: "revised"
        }
      });
    }
    if (path.endsWith("/map/config")) {
      return jsonResponse({ provider: "amap", enabled: true, jsApiKey: "web-key" });
    }
    if (path.endsWith("/agent/sessions")) {
      return jsonResponse(agentSession());
    }
    if (path.endsWith("/agent/sessions/sess_pref/messages")) {
      return jsonResponse(agentMessageResponse());
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);

  fireEvent.doubleClick(screen.getByRole("button", { name: "旅行偏好卡片" }));
  await waitFor(() => expect(screen.getByText("喜欢轻松不赶路。")).toBeTruthy());
  expect(screen.queryByText("需要确认")).toBeNull();
  fireEvent.click(screen.getByText("编辑偏好卡片"));
  await waitFor(() => expect(screen.getByLabelText("旅行偏好 Markdown")).toBeTruthy());
  expect(screen.queryByText("待提取")).toBeNull();
  expect(screen.queryByText("偏好对话")).toBeNull();
  expect(screen.queryByText("提取偏好")).toBeNull();
  fireEvent.change(screen.getByLabelText("旅行偏好 Markdown"), {
    target: { value: "# 我的旅行偏好\n\n## 旅行节奏\n- 用户偏好慢节奏。\n\n## 预算偏好\n- 预算约 3500 元。\n" }
  });
  fireEvent.click(screen.getByText("保存偏好卡片"));
  await waitFor(() => expect(screen.getByText("已保存，后续规划会读取这份偏好卡片。")).toBeTruthy());

  fireEvent.change(screen.getByLabelText("Agent 对话文本"), {
    target: { value: "两个大人一个老人，预算 3000 左右，不想太赶，少换乘，北京 故宫博物院 拍照" }
  });
  fireEvent.click(screen.getByText("发送给 Agent"));

  await waitFor(() => expect(screen.getByText("北京地图行程草案")).toBeTruthy());
  const messageCall = fetchMock.mock.calls.find((call) => String(call[0]).endsWith("/agent/sessions/sess_pref/messages"));
  expect(messageCall).toBeTruthy();
  const messageBody = JSON.parse(String(messageCall?.[1]?.body));
  expect(messageBody.context.memoryText).toContain("预算约 3500 元");
  expect(messageBody.context.currentPreferenceSummary).toContain("预算约 3500 元");
  expect(messageBody.context.preferenceCard.summaryText).toContain("预算约 3500 元");
  expect(messageBody.context.preferenceCard.partySize).toBeUndefined();
  expect(messageBody.context.preferenceCardId).toBe("card_1");
});

test("preference card opens with one touch or keyboard and closes without saving", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: RequestInfo | URL) => {
      const path = String(url);
      if (path.endsWith("/providers/status")) {
        return jsonResponse({ mode: "mock", default: [], mock: [] });
      }
      if (path.endsWith("/agent/sessions/current")) {
        return jsonResponse(agentSession(preferenceMemory("# 我的旅行偏好\n\n## 旅行节奏\n- 喜欢轻松不赶路。\n")));
      }
      return jsonResponse({}, 404);
    })
  );

  render(<AppShell />);

  const trigger = screen.getByRole("button", { name: "旅行偏好卡片" });
  const workspaceMeta = trigger.closest(".workspace-meta");
  expect(workspaceMeta).toBeTruthy();
  expect(trigger.getAttribute("aria-expanded")).toBe("false");
  expect(trigger.querySelector("svg")).toBeTruthy();
  expect(screen.queryByLabelText("旅行偏好详情")).toBeNull();

  fireEvent.click(trigger);
  await waitFor(() => expect(screen.getByLabelText("旅行偏好详情")).toBeTruthy());
  expect(screen.getByText("喜欢轻松不赶路。")).toBeTruthy();
  expect(screen.getByText("可手动编辑")).toBeTruthy();
  expect(screen.queryByText("允许自动更新")).toBeNull();
  expect(trigger.getAttribute("aria-expanded")).toBe("true");

  fireEvent.click(trigger);
  expect(screen.queryByLabelText("旅行偏好详情")).toBeNull();
  fireEvent.click(trigger);
  expect(screen.getByLabelText("旅行偏好详情")).toBeTruthy();

  fireEvent.keyDown(window, { key: "Escape" });
  expect(screen.queryByLabelText("旅行偏好详情")).toBeNull();

  fireEvent.keyDown(trigger, { key: "Enter" });
  expect(screen.getByLabelText("旅行偏好详情")).toBeTruthy();
  fireEvent.keyDown(trigger, { key: "Enter" });
  expect(screen.queryByLabelText("旅行偏好详情")).toBeNull();

  fireEvent.keyDown(trigger, { key: " " });
  expect(screen.getByLabelText("旅行偏好详情")).toBeTruthy();

  const requestCount = vi.mocked(fetch).mock.calls.length;
  fireEvent.pointerDown(screen.getByText("喜欢轻松不赶路。"));
  expect(screen.getByLabelText("旅行偏好详情")).toBeTruthy();
  fireEvent.pointerDown(document.body);
  expect(screen.queryByLabelText("旅行偏好详情")).toBeNull();
  expect(trigger.getAttribute("aria-expanded")).toBe("false");
  expect(vi.mocked(fetch).mock.calls.length).toBe(requestCount);

  fireEvent.pointerDown(trigger);
  fireEvent.click(trigger);
  expect(screen.getByLabelText("旅行偏好详情")).toBeTruthy();
  expect(screen.queryByRole("button", { name: "收起旅行偏好" })).toBeNull();
});

test("preference memory updates do not auto-open the collapsed top-bar pin", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: RequestInfo | URL) => {
      const path = String(url);
      if (path.endsWith("/providers/status")) {
        return jsonResponse({ mode: "mock", default: [], mock: [] });
      }
      if (path.endsWith("/agent/sessions/current")) {
        return jsonResponse(agentSession(preferenceMemory("# 我的旅行偏好\n\n## 旅行节奏\n- 初始偏好。\n")));
      }
      return jsonResponse({}, 404);
    })
  );

  render(<AppShell />);

  const trigger = screen.getByRole("button", { name: "旅行偏好卡片" });
  expect(trigger.getAttribute("aria-expanded")).toBe("false");
  plannerStore.setState({
    preferenceMemory: preferenceMemory("# 我的旅行偏好\n\n## 旅行节奏\n- 新加载的偏好内容。\n")
  });

  await waitFor(() => expect(trigger.getAttribute("aria-expanded")).toBe("false"));
  expect(screen.queryByLabelText("旅行偏好详情")).toBeNull();
});

test("empty preference memory shows empty state and is not sent to agent context", async () => {
  const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) {
      return jsonResponse({ mode: "mock", default: [], mock: [] });
    }
    if (path.endsWith("/agent/sessions/current")) {
      return jsonResponse(agentSession(preferenceMemory("# 我的旅行偏好\n\n## 旅行节奏\n- 暂无明确记录。\n\n## 交通偏好\n- 暂无明确记录。\n")));
    }
    if (path.includes("/preferences/memory") && (!init || init.method === undefined || init.method === "GET")) {
      return jsonResponse(preferenceMemory("# 我的旅行偏好\n\n## 旅行节奏\n- 暂无明确记录。\n\n## 交通偏好\n- 暂无明确记录。\n"));
    }
    if (path.endsWith("/preferences/extract")) {
      return jsonResponse({ summaryCard: preferenceCard("") });
    }
    if (path.endsWith("/agent/sessions")) {
      return jsonResponse(agentSession());
    }
    if (path.endsWith("/agent/sessions/sess_pref/messages")) {
      return jsonResponse(agentMessageResponse());
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);

  fireEvent.doubleClick(screen.getByRole("button", { name: "旅行偏好卡片" }));
  await waitFor(() =>
    expect(screen.getByText("还没有记录明确旅行偏好，Agent 会在后续对话中逐步整理。")).toBeTruthy()
  );
  expect(screen.queryByText("暂无明确记录。")).toBeNull();

  fireEvent.change(screen.getByLabelText("Agent 对话文本"), {
    target: { value: "北京一天，先按默认规划" }
  });
  fireEvent.click(screen.getByText("发送给 Agent"));

  await waitFor(() => expect(screen.getByText("北京地图行程草案")).toBeTruthy());
  const messageCall = fetchMock.mock.calls.find((call) => String(call[0]).endsWith("/agent/sessions/sess_pref/messages"));
  const messageBody = JSON.parse(String(messageCall?.[1]?.body));
  expect(messageBody.context.memoryText).toBe("");
  expect(messageBody.context.currentPreferenceSummary).toBe("");
  expect(JSON.stringify(messageBody.context)).not.toContain("暂无明确记录");
  expect(JSON.stringify(messageBody.context)).not.toContain("# 我的旅行偏好");
});

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" }
  });
}

function firePreferencePointer(
  target: HTMLElement,
  type: "pointerdown" | "pointermove" | "pointerup",
  values: { pointerId: number; clientX: number; clientY: number }
) {
  const event = new Event(type, { bubbles: true });
  Object.defineProperty(event, "pointerId", { value: values.pointerId });
  Object.defineProperty(event, "clientX", { value: values.clientX });
  Object.defineProperty(event, "clientY", { value: values.clientY });
  fireEvent(target, event);
}

function sessionSummary(sessionId: string, title: string, updatedAt: string) {
  return {
    sessionId,
    status: "active",
    city: "北京",
    title,
    activePlanId: `plan_${sessionId}`,
    activeVersionId: sessionId === "sess_with_risk" ? "ver_risk" : null,
    turnCount: 0,
    updatedAt,
    createdAt: updatedAt
  };
}

function agentSession(memory = preferenceMemory(""), itinerary: Record<string, unknown> | null = null, sessionId = "sess_pref") {
  return {
    sessionId,
    status: "active",
    city: "北京",
    title: "北京 AI 行程",
    activePlanId: String(itinerary?.id ?? "plan_pref"),
    activeVersionId: sessionId === "sess_with_risk" ? "ver_risk" : itinerary ? "ver_pref" : null,
    turns: [],
    itinerary,
    pendingPoiCandidates: [],
    preferenceMemory: memory
  };
}

function preferenceMemory(memoryText: string, autoUpdateEnabled = true) {
  return {
    userId: "local",
    memoryText,
    autoUpdateEnabled,
    createdAt: "2026-06-10T10:00:00Z",
    updatedAt: "2026-06-10T10:00:00Z"
  };
}

function preferenceCard(summaryText: string) {
  return {
    id: "card_1",
    profileId: "pref_1",
    partySize: 2,
    travelerTypes: ["adult"],
    budgetRange: "",
    pacePreference: "",
    summaryText,
    items: [],
    status: "draft"
  };
}

function agentMessageResponse() {
  const createdAt = "2026-06-10T10:00:00Z";
  return {
    userTurn: {
      id: "turn_pref_user",
      role: "user",
      content: "两个大人一个老人，预算 3000 左右，不想太赶，少换乘，北京 故宫博物院 拍照",
      turnIndex: 1,
      status: "active",
      createdAt,
      updatedAt: createdAt
    },
    assistantTurn: {
      id: "turn_pref_assistant",
      role: "assistant",
      content: "已按偏好生成行程。",
      turnIndex: 2,
      status: "active",
      itineraryVersionId: "ver_pref",
      createdAt,
      updatedAt: createdAt
    },
    itinerary: itineraryPlan(),
    version: { id: "ver_pref", versionNumber: 1, sourceType: "agent" },
    pendingPoiCandidates: [],
    warnings: []
  };
}

function itineraryPlan() {
  return {
    id: "plan_pref",
    title: "北京地图行程草案",
    city: "北京",
    templateType: "custom",
    budgetTarget: 3000,
    budgetEstimate: 120,
    budgetDeltaExplanation: "已参考偏好预算。",
    decisionRationale: "已应用偏好：轻松不赶路。",
    status: "draft",
    days: [],
    routeOptions: [],
    weatherSignals: [],
    trafficCrowdingSignals: [],
    ticketLookupResults: []
  };
}

function itineraryPlanWithRisk() {
  return {
    ...itineraryPlan(),
    id: "plan_risk",
    title: "北京风险行程",
    activeVersionId: "ver_risk",
    days: [
      {
        id: "day_risk_1",
        dayNumber: 1,
        title: "Day 1",
        weatherSummary: "",
        riskSummary: "需要提前预约。",
        totalEstimatedCost: 0,
        segments: [
          {
            id: "seg_risk_1",
            startTime: "09:00",
            endTime: "11:00",
            kind: "visit",
            poi: {
              id: "poi_risk_1",
              name: "故宫博物院",
              city: "北京",
              category: "museum",
              latitude: 39.916,
              longitude: 116.397,
              source: "amap-place-search",
              confidence: 0.95,
              groundingStatus: "verified_amap",
              mapReady: true,
              routeable: true
            },
            transportMode: "transit",
            estimatedCost: 0,
            notes: "注意预约。"
          }
        ]
      }
    ],
    poiRiskAlerts: [
      {
        id: "risk_palace",
        planId: "plan_risk",
        segmentId: "seg_risk_1",
        poiName: "故宫博物院",
        status: "available",
        summary: "故宫节假日预约紧张，建议提前确认官方预约。",
        sourceName: "故宫官方公告",
        sourceUrl: "https://example.com/palace",
        sources: [{ title: "故宫官方公告", url: "https://example.com/palace", snippet: "节假日预约紧张。" }],
        confidence: 0.8,
        failureReason: null,
        userVisibleCaveat: "以官方公告为准。",
        queriedAt: "2026-06-10T10:00:00Z"
      }
    ]
  };
}

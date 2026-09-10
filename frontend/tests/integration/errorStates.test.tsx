import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { AppShell } from "../../src/components/AppShell";
import { DailyTimeline } from "../../src/components/timeline/DailyTimeline";
import { plannerStore } from "../../src/state/plannerStore";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  plannerStore.setState({ itineraryPlan: null, agentSession: null, conversationTurns: [], activeVersionId: null });
});

test("timeline shows an empty state when a plan has no days", () => {
  render(
    <DailyTimeline
      plan={{
        id: "plan_empty",
        title: "空行程",
        city: "北京",
        templateType: "custom",
        budgetEstimate: 0,
        budgetDeltaExplanation: "",
        decisionRationale: "",
        status: "draft",
        days: [],
        routeOptions: [],
        weatherSignals: [],
        trafficCrowdingSignals: [],
        ticketLookupResults: []
      }}
      selectedDayNumber={1}
      selectedSegmentId={null}
      onSelectSegment={() => undefined}
      onTransportChange={() => undefined}
    />
  );

  expect(screen.getByText("行程时间轴待生成。")).toBeTruthy();
});

test("uploaded inspiration comparison failure is visible but does not hide generated itinerary", async () => {
  const fetchMock = vi.fn(async (url: RequestInfo | URL, _init?: RequestInit) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) {
      return jsonResponse({ mode: "mock", default: [], mock: [] });
    }
    if (path.endsWith("/source-materials/upload")) {
      return jsonResponse({ sourceMaterialId: "mat_uploaded", kind: "screenshot", thumbnailPlaceholder: true,
        originalRetention: "temporary_cache", cacheStatus: "retained" });
    }
    if (path.endsWith("/inspirations")) {
      return jsonResponse({ inspirationSetId: "insp_error", status: "extracting", sourceMaterialIds: ["mat_text"] });
    }
    if (path.endsWith("/inspirations/insp_error/extract")) {
      return jsonResponse(extractionResponse());
    }
    if (path.endsWith("/itineraries/generate")) {
      return jsonResponse(itineraryEnvelope());
    }
    if (path.endsWith("/itineraries/compare")) {
      return jsonResponse({ detail: "ticket provider unavailable" }, 503);
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);
  fireEvent.change(screen.getByLabelText("Agent 对话文本"), { target: { value: "北京 故宫博物院" } });
  fireEvent.drop(screen.getByRole("form", { name: "Agent 对话输入" }), {
    dataTransfer: { files: [new File(["image"], "guide.png", { type: "image/png" })], getData: () => "" }
  });
  fireEvent.click(screen.getByText("发送给 Agent"));

  await waitFor(() => expect(screen.getByText("北京地图行程草案")).toBeTruthy());
  expect(screen.getByText("ticket provider unavailable")).toBeTruthy();
  expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith("/source-materials/upload"))).toBe(true);
  const createRequest = fetchMock.mock.calls.find(([url]) => String(url).endsWith("/inspirations"));
  expect(JSON.parse(String(createRequest?.[1]?.body))).toMatchObject({
    socialLinks: [], sourceMaterialIds: ["mat_uploaded"]
  });
});

test("agent failure is visible and does not pollute the current itinerary", async () => {
  plannerStore.setState({ itineraryPlan: itineraryWithDay(), activeVersionId: "ver_existing" });
  vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) {
      return jsonResponse({ mode: "mock", default: [], mock: [] });
    }
    if (path.endsWith("/preferences/extract")) {
      return jsonResponse({ detail: "preference fallback" }, 500);
    }
    if (path.endsWith("/map/config")) {
      return jsonResponse({ provider: "amap", enabled: true, jsApiKey: "web-key" });
    }
    if (path.endsWith("/agent/sessions")) {
      return jsonResponse({
        sessionId: "sess_error",
        status: "active",
        city: "北京",
        title: "北京 AI 行程",
        activePlanId: "plan_existing",
        activeVersionId: "ver_existing",
        turns: [],
        itinerary: itineraryWithDay(),
        pendingPoiCandidates: []
      });
    }
    if (path.endsWith("/agent/sessions/sess_error/messages")) {
      const createdAt = "2026-06-10T10:00:00Z";
      return jsonResponse({
        userTurn: {
          id: "turn_error_user",
          role: "user",
          content: "移动到冲突时间",
          turnIndex: 1,
          status: "active",
          createdAt,
          updatedAt: createdAt
        },
        assistantTurn: {
          id: "turn_error_assistant",
          role: "assistant",
          content: "Agent 修改未通过校验，行程未更新。",
          turnIndex: 2,
          status: "failed",
          createdAt,
          updatedAt: createdAt
        },
        itinerary: null,
        version: null,
        pendingPoiCandidates: [],
        warnings: ["Segment time conflicts with another segment"]
      });
    }
    return jsonResponse({}, 404);
  }));

  render(<AppShell />);
  expect(screen.getAllByText("现有行程").length).toBeGreaterThan(0);
  fireEvent.change(screen.getByLabelText("Agent 对话文本"), { target: { value: "移动到冲突时间" } });
  fireEvent.click(screen.getByText("发送给 Agent"));

  await waitFor(() => expect(screen.getByText("Agent 修改未通过校验，行程未更新。")).toBeTruthy());
  expect(screen.getByText("Segment time conflicts with another segment")).toBeTruthy();
  expect(plannerStore.getSnapshot().itineraryPlan?.title).toBe("现有行程");
});

test("agent structured validation errors are shown as a friendly message", async () => {
  plannerStore.setState({ itineraryPlan: itineraryWithDay(), activeVersionId: "ver_existing" });
  vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) {
      return jsonResponse({ mode: "mock", default: [], mock: [] });
    }
    if (path.endsWith("/preferences/extract")) {
      return jsonResponse({ detail: "preference fallback" }, 500);
    }
    if (path.endsWith("/map/config")) {
      return jsonResponse({ provider: "amap", enabled: true, jsApiKey: "web-key" });
    }
    if (path.endsWith("/agent/sessions")) {
      return jsonResponse({
        sessionId: "sess_validation",
        status: "active",
        city: "北京",
        title: "北京 AI 行程",
        activePlanId: "plan_existing",
        activeVersionId: "ver_existing",
        turns: [],
        itinerary: itineraryWithDay(),
        pendingPoiCandidates: []
      });
    }
    if (path.endsWith("/agent/sessions/sess_validation/messages")) {
      const createdAt = "2026-06-10T10:00:00Z";
      return jsonResponse({
        userTurn: {
          id: "turn_validation_user",
          role: "user",
          content: "安排北京两天",
          turnIndex: 1,
          status: "active",
          createdAt,
          updatedAt: createdAt
        },
        assistantTurn: {
          id: "turn_validation_assistant",
          role: "assistant",
          content: "Agent 输出无效，未更新行程。",
          turnIndex: 2,
          status: "failed",
          createdAt,
          updatedAt: createdAt
        },
        itinerary: null,
        version: null,
        pendingPoiCandidates: [],
        warnings: [
          "3 validation errors for AgentStructuredOutput\nfullItinerary.title\n  Field required\npoiResolutionRequests.0.name\n  Field required"
        ]
      });
    }
    return jsonResponse({}, 404);
  }));

  render(<AppShell />);
  fireEvent.change(screen.getByLabelText("Agent 对话文本"), { target: { value: "安排北京两天" } });
  fireEvent.click(screen.getByText("发送给 Agent"));

  await waitFor(() =>
    expect(screen.getByText("Agent 返回的结构化行程格式不符合要求，已拒绝更新行程。")).toBeTruthy()
  );
  expect(screen.queryByText(/fullItinerary\.title/)).toBeNull();
  expect(screen.queryByText(/Field required/)).toBeNull();
  expect(screen.queryByRole("button", { name: "继续" })).toBeNull();
  expect(plannerStore.getSnapshot().itineraryPlan?.title).toBe("现有行程");
});

test("failed assistant turn only shows resume for backend-declared resumable failures", async () => {
  const createdAt = "2026-06-10T10:00:00Z";
  plannerStore.setState({
    agentSession: {
      sessionId: "sess_resume_visible",
      status: "active",
      city: "北京",
      title: "北京 AI 行程",
      activePlanId: "plan_existing",
      activeVersionId: "ver_existing",
      turns: [],
      itinerary: itineraryWithDay(),
      pendingPoiCandidates: []
    },
    conversationTurns: [
      conversationTurn("turn_u1", "user", "继续上一轮", 1, "active", "ver_existing", createdAt),
      conversationTurn(
        "turn_a1",
        "assistant",
        "Agent 主循环失败。",
        2,
        "failed",
        "ver_existing",
        createdAt,
        "tool_loop_failed Agent provider timeout"
      )
    ],
    itineraryPlan: itineraryWithDay(),
    activeVersionId: "ver_existing"
  });
  vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) {
      return jsonResponse({ mode: "mock", default: [], mock: [] });
    }
    if (path.endsWith("/map/config")) {
      return jsonResponse({ provider: "amap", enabled: true, jsApiKey: "web-key" });
    }
    return jsonResponse({}, 404);
  }));

  render(<AppShell />);

  expect(await screen.findByRole("button", { name: "继续" })).toBeTruthy();
});

test("failed assistant turn does not show resume for event-only resumable markers", async () => {
  const createdAt = "2026-06-10T10:00:00Z";
  plannerStore.setState({
    agentSession: {
      sessionId: "sess_resume_hidden",
      status: "active",
      city: "北京",
      title: "北京 AI 行程",
      activePlanId: "plan_existing",
      activeVersionId: "ver_existing",
      turns: [],
      itinerary: itineraryWithDay(),
      pendingPoiCandidates: []
    },
    conversationTurns: [
      conversationTurn("turn_u1", "user", "继续上一轮", 1, "active", "ver_existing", createdAt),
      {
        ...conversationTurn(
          "turn_a1",
          "assistant",
          "Agent 输出无效。",
          2,
          "failed",
          "ver_existing",
          createdAt,
          "schema validation failed"
        ),
        planningSteps: [
          {
            type: "tool",
            label: "POI grounding",
            status: "failed",
            detail: "semantic_candidate_hint_missing",
            fallbackUsed: false,
            durationMs: 0,
            metadata: { resultState: "semantic_candidate_hint_missing" },
            timestamp: createdAt
          }
        ]
      }
    ],
    itineraryPlan: itineraryWithDay(),
    activeVersionId: "ver_existing"
  });
  vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) {
      return jsonResponse({ mode: "mock", default: [], mock: [] });
    }
    if (path.endsWith("/map/config")) {
      return jsonResponse({ provider: "amap", enabled: true, jsApiKey: "web-key" });
    }
    return jsonResponse({}, 404);
  }));

  render(<AppShell />);

  await waitFor(() => expect(screen.getByText("Agent 输出无效。")).toBeTruthy());
  expect(screen.queryByRole("button", { name: "继续" })).toBeNull();
});

test("agent quota errors are shown as a friendly message", async () => {
  plannerStore.setState({ itineraryPlan: itineraryWithDay(), activeVersionId: "ver_existing" });
  vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) {
      return jsonResponse({ mode: "mock", default: [], mock: [] });
    }
    if (path.endsWith("/preferences/extract")) {
      return jsonResponse({ detail: "preference fallback" }, 500);
    }
    if (path.endsWith("/map/config")) {
      return jsonResponse({ provider: "amap", enabled: true, jsApiKey: "web-key" });
    }
    if (path.endsWith("/agent/sessions")) {
      return jsonResponse({
        sessionId: "sess_quota",
        status: "active",
        city: "北京",
        title: "北京 AI 行程",
        activePlanId: "plan_existing",
        activeVersionId: "ver_existing",
        turns: [],
        itinerary: itineraryWithDay(),
        pendingPoiCandidates: []
      });
    }
    if (path.endsWith("/agent/sessions/sess_quota/messages")) {
      return jsonResponse({ detail: "CUQPS_HAS_EXCEEDED_THE_LIMIT" }, 502);
    }
    return jsonResponse({}, 404);
  }));

  render(<AppShell />);
  fireEvent.change(screen.getByLabelText("Agent 对话文本"), { target: { value: "安排北京一天" } });
  fireEvent.click(screen.getByText("发送给 Agent"));

  await waitFor(() => expect(screen.getByText("高德 POI 查询频率超限，请稍后重试。行程未写入。")).toBeTruthy());
  expect(plannerStore.getSnapshot().itineraryPlan?.title).toBe("现有行程");
});

test("pending POI evidence stays in state without rendering the removed global selector", async () => {
  const createdAt = "2026-06-10T10:00:00Z";
  plannerStore.setState({
    conversationTurns: [conversationTurn("turn_u1", "user", "加一个餐厅", 1, "active", undefined, createdAt)],
    pendingPoiCandidates: [
      {
        id: "cand_1",
        query: "老胡同餐厅",
        city: "北京",
        category: "food",
        status: "pending",
        candidates: [
          mapPoi("poi_1", "老胡同餐厅(东城店)"),
          mapPoi("poi_2", "胡同餐厅")
        ],
        createdAt
      }
    ]
  });
  vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) {
      return jsonResponse({ mode: "mock", default: [], mock: [] });
    }
    if (path.endsWith("/map/config")) {
      return jsonResponse({ provider: "amap", enabled: true, jsApiKey: "web-key" });
    }
    return jsonResponse({}, 404);
  }));

  render(<AppShell />);

  expect(screen.queryByText(/正在选择 POI/)).toBeNull();
  expect(screen.queryByText(/已加入 Day1 0 个/)).toBeNull();
  expect(screen.queryByText("确认加入行程")).toBeNull();
  expect(screen.queryByText(/来源：amap-place-search · 置信度/)).toBeNull();
  expect(screen.queryByText("老胡同餐厅(东城店)")).toBeNull();
  expect(screen.queryByText("胡同餐厅")).toBeNull();
  expect(plannerStore.getSnapshot().pendingPoiCandidates[0]?.id).toBe("cand_1");
});

test("editing a previous user message supersedes later turns and rolls back the itinerary", async () => {
  const createdAt = "2026-06-10T10:00:00Z";
  plannerStore.setState({
    agentSession: {
      sessionId: "sess_edit",
      status: "active",
      city: "北京",
      title: "北京 AI 行程",
      activePlanId: "plan_existing",
      activeVersionId: "ver_2",
      turns: [],
      itinerary: itineraryWithTitle("第二版行程"),
      pendingPoiCandidates: []
    },
    conversationTurns: [
      conversationTurn("turn_u1", "user", "安排北京两天", 1, "active", "ver_1", createdAt),
      conversationTurn("turn_a1", "assistant", "已生成第一版。", 2, "active", "ver_1", createdAt),
      conversationTurn("turn_u2", "user", "改成第二版", 3, "active", "ver_2", createdAt),
      conversationTurn("turn_a2", "assistant", "已生成第二版。", 4, "active", "ver_2", createdAt)
    ],
    itineraryPlan: itineraryWithTitle("第二版行程"),
    activeVersionId: "ver_2",
    pendingPoiCandidates: []
  });
  const editResponse = deferred<Response>();
  const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) {
      return jsonResponse({ mode: "mock", default: [], mock: [] });
    }
    if (path.endsWith("/map/config")) {
      return jsonResponse({ provider: "amap", enabled: true, jsApiKey: "web-key" });
    }
    if (path.endsWith("/agent/sessions/sess_edit/messages/turn_u1") && init?.method === "PATCH") {
      return editResponse.promise;
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);

  fireEvent.click(screen.getByRole("button", { name: "编辑消息 1" }));
  fireEvent.change(screen.getByLabelText("编辑消息 1"), {
    target: { value: "改成北京三天，别安排太满" }
  });
  fireEvent.click(screen.getByText("保存编辑"));
  expect(screen.getByText("保存中")).toBeTruthy();
  expect(screen.queryByLabelText("Agent 处理过程")).toBeNull();
  expect(screen.queryByText("正在保存编辑并重新规划")).toBeNull();
  expect(screen.queryByText("正在保存编辑、恢复对应版本并基于新消息重新生成。")).toBeNull();

  editResponse.resolve(jsonResponse({
    editedTurn: {
      ...conversationTurn("turn_u1_edited", "user", "改成北京三天，别安排太满", 5, "active", "ver_3", createdAt),
      parentTurnId: "turn_u1"
    },
    supersededTurnIds: ["turn_u1", "turn_a1", "turn_u2", "turn_a2"],
    restoredVersionId: "ver_1",
    assistantTurn: conversationTurn("turn_a3", "assistant", "已从这条消息重新生成。", 6, "active", "ver_3", createdAt),
    itinerary: itineraryWithTitle("重写后的三天行程"),
    version: { id: "ver_3", versionNumber: 3, sourceType: "agent" },
    pendingPoiCandidates: [],
    warnings: []
  }));

  await waitFor(() => expect(screen.getByText("已从这条消息重新生成。")).toBeTruthy());
  expect(screen.getAllByText("重写后的三天行程").length).toBeGreaterThan(0);
  expect(screen.getAllByText("已被新分支取代").length).toBe(4);
  expect(plannerStore.getSnapshot().activeVersionId).toBe("ver_3");
  expect(plannerStore.getSnapshot().itineraryPlan?.title).toBe("重写后的三天行程");
  expect(plannerStore.getSnapshot().conversationTurns.find((turn) => turn.id === "turn_u1")?.status).toBe(
    "superseded"
  );
  expect(plannerStore.getSnapshot().conversationTurns.find((turn) => turn.id === "turn_u1_edited")?.content).toBe(
    "改成北京三天，别安排太满"
  );
});

test("stopping an edit rerun aborts PATCH, clears saving state, and ignores a late response", async () => {
  const createdAt = "2026-06-10T10:00:00Z";
  plannerStore.setState({
    agentSession: {
      sessionId: "sess_edit_cancel",
      status: "active",
      city: "北京",
      title: "北京 AI 行程",
      activePlanId: "plan_existing",
      activeVersionId: "ver_1",
      turns: [],
      itinerary: itineraryWithTitle("原行程"),
      pendingPoiCandidates: []
    },
    conversationTurns: [
      conversationTurn("turn_u1", "user", "安排北京两天", 1, "active", "ver_1", createdAt),
      conversationTurn("turn_a1", "assistant", "已生成原行程。", 2, "active", "ver_1", createdAt)
    ],
    itineraryPlan: itineraryWithTitle("原行程"),
    activeVersionId: "ver_1",
    pendingPoiCandidates: []
  });
  const editResponse = deferred<Response>();
  let editSignal: AbortSignal | undefined;
  const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) {
      return jsonResponse({ mode: "mock", default: [], mock: [] });
    }
    if (path.endsWith("/map/config")) {
      return jsonResponse({ provider: "amap", enabled: true, jsApiKey: "web-key" });
    }
    if (path.endsWith("/agent/sessions/sess_edit_cancel/messages/turn_u1") && init?.method === "PATCH") {
      editSignal = init.signal ?? undefined;
      return editResponse.promise;
    }
    if (path.endsWith("/agent/sessions/sess_edit_cancel/runs/cancel") && init?.method === "POST") {
      return jsonResponse({ cancelRequested: true });
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);
  fireEvent.click(screen.getByRole("button", { name: "编辑消息 1" }));
  fireEvent.change(screen.getByLabelText("编辑消息 1"), { target: { value: "改成北京三天" } });
  fireEvent.click(screen.getByText("保存编辑"));

  expect(await screen.findByRole("button", { name: "停止本轮" })).toBeTruthy();
  expect(editSignal).toBeInstanceOf(AbortSignal);
  fireEvent.click(screen.getByRole("button", { name: "停止本轮" }));

  await waitFor(() =>
    expect(
      fetchMock.mock.calls.some(
        ([url, init]) =>
          String(url).endsWith("/agent/sessions/sess_edit_cancel/runs/cancel") &&
          (init as RequestInit | undefined)?.method === "POST"
      )
    ).toBe(true)
  );
  expect(editSignal?.aborted).toBe(true);
  expect(screen.queryByText("保存中")).toBeNull();
  expect(screen.queryByRole("button", { name: "停止本轮" })).toBeNull();

  editResponse.resolve(jsonResponse({
    editedTurn: {
      ...conversationTurn("turn_u1_edited", "user", "改成北京三天", 3, "active", "ver_late", createdAt),
      parentTurnId: "turn_u1"
    },
    supersededTurnIds: ["turn_u1", "turn_a1"],
    restoredVersionId: "ver_1",
    assistantTurn: conversationTurn("turn_a_late", "assistant", "迟到响应不应展示。", 4, "active", "ver_late", createdAt),
    itinerary: itineraryWithTitle("迟到行程"),
    version: { id: "ver_late", versionNumber: 2, sourceType: "agent" },
    pendingPoiCandidates: [],
    warnings: []
  }));

  await editResponse.promise;
  await Promise.resolve();
  expect(screen.queryByText("迟到响应不应展示。")).toBeNull();
  expect(plannerStore.getSnapshot().activeVersionId).toBe("ver_1");
  expect(plannerStore.getSnapshot().itineraryPlan?.title).toBe("原行程");
});

test("stopping a resumed run aborts POST, clears generation state, and ignores a late response", async () => {
  const createdAt = "2026-06-10T10:00:00Z";
  plannerStore.setState({
    agentSession: {
      sessionId: "sess_resume_cancel",
      status: "active",
      city: "北京",
      title: "北京 AI 行程",
      activePlanId: "plan_existing",
      activeVersionId: "ver_1",
      turns: [],
      itinerary: itineraryWithTitle("原行程"),
      pendingPoiCandidates: []
    },
    conversationTurns: [
      conversationTurn("turn_u1", "user", "继续上一轮", 1, "active", "ver_1", createdAt),
      conversationTurn(
        "turn_a1",
        "assistant",
        "Agent 主循环失败。",
        2,
        "failed",
        "ver_1",
        createdAt,
        "tool_loop_failed Agent provider timeout"
      )
    ],
    itineraryPlan: itineraryWithTitle("原行程"),
    activeVersionId: "ver_1",
    pendingPoiCandidates: []
  });
  const resumeResponse = deferred<Response>();
  let resumeSignal: AbortSignal | undefined;
  const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) {
      return jsonResponse({ mode: "mock", default: [], mock: [] });
    }
    if (path.endsWith("/map/config")) {
      return jsonResponse({ provider: "amap", enabled: true, jsApiKey: "web-key" });
    }
    if (path.endsWith("/agent/sessions/sess_resume_cancel/turns/turn_a1/resume") && init?.method === "POST") {
      resumeSignal = init.signal ?? undefined;
      return resumeResponse.promise;
    }
    if (path.endsWith("/agent/sessions/sess_resume_cancel/runs/cancel") && init?.method === "POST") {
      return jsonResponse({ cancelRequested: true });
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);
  fireEvent.click(await screen.findByRole("button", { name: "继续" }));

  expect(await screen.findByRole("button", { name: "停止本轮" })).toBeTruthy();
  expect(resumeSignal).toBeInstanceOf(AbortSignal);
  fireEvent.click(screen.getByRole("button", { name: "停止本轮" }));

  await waitFor(() =>
    expect(
      fetchMock.mock.calls.some(
        ([url, init]) =>
          String(url).endsWith("/agent/sessions/sess_resume_cancel/runs/cancel") &&
          (init as RequestInit | undefined)?.method === "POST"
      )
    ).toBe(true)
  );
  expect(resumeSignal?.aborted).toBe(true);
  expect(screen.queryByRole("button", { name: "停止本轮" })).toBeNull();
  expect(screen.getByRole("button", { name: "继续" })).toBeTruthy();

  resumeResponse.resolve(jsonResponse({
    userTurn: conversationTurn("turn_u_late", "user", "续跑", 3, "active", "ver_late", createdAt),
    assistantTurn: conversationTurn("turn_a_late", "assistant", "迟到续跑响应不应展示。", 4, "active", "ver_late", createdAt),
    itinerary: itineraryWithTitle("迟到续跑行程"),
    version: { id: "ver_late", versionNumber: 2, sourceType: "agent" },
    pendingPoiCandidates: [],
    warnings: []
  }));

  await resumeResponse.promise;
  await Promise.resolve();
  expect(screen.queryByText("迟到续跑响应不应展示。")).toBeNull();
  expect(plannerStore.getSnapshot().activeVersionId).toBe("ver_1");
  expect(plannerStore.getSnapshot().itineraryPlan?.title).toBe("原行程");
});

test("editing a user message without a version can regenerate from backend response", async () => {
  const createdAt = "2026-06-10T10:00:00Z";
  plannerStore.setState({
    agentSession: {
      sessionId: "sess_no_version",
      status: "active",
      city: "北京",
      title: "北京 AI 行程",
      activePlanId: "plan_existing",
      activeVersionId: null,
      turns: [],
      itinerary: null,
      pendingPoiCandidates: []
    },
    conversationTurns: [
      conversationTurn("turn_u1", "user", "失败消息", 1, "active", undefined, createdAt),
      conversationTurn("turn_a1", "assistant", "Agent 输出无效，未更新行程。", 2, "failed", undefined, createdAt)
    ],
    itineraryPlan: null,
    activeVersionId: null,
    pendingPoiCandidates: []
  });
  vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) {
      return jsonResponse({ mode: "mock", default: [], mock: [] });
    }
    if (path.endsWith("/map/config")) {
      return jsonResponse({ provider: "amap", enabled: true, jsApiKey: "web-key" });
    }
    if (path.endsWith("/agent/sessions/sess_no_version/messages/turn_u1") && init?.method === "PATCH") {
      return jsonResponse({
        editedTurn: {
          ...conversationTurn("turn_u1_edited", "user", "只安排故宫博物院", 3, "active", "ver_1", createdAt),
          parentTurnId: "turn_u1"
        },
        supersededTurnIds: ["turn_u1", "turn_a1"],
        restoredVersionId: null,
        assistantTurn: conversationTurn("turn_a2", "assistant", "已重新生成。", 4, "active", "ver_1", createdAt),
        itinerary: itineraryWithTitle("北京故宫 1 日游"),
        version: { id: "ver_1", versionNumber: 1, sourceType: "agent" },
        pendingPoiCandidates: [],
        warnings: []
      });
    }
    return jsonResponse({}, 404);
  }));

  render(<AppShell />);

  fireEvent.click(screen.getByRole("button", { name: "编辑消息 1" }));
  fireEvent.change(screen.getByLabelText("编辑消息 1"), { target: { value: "只安排故宫博物院" } });
  fireEvent.click(screen.getByText("保存编辑"));

  await waitFor(() => expect(screen.getByText("已重新生成。")).toBeTruthy());
  expect(screen.getAllByText("北京故宫 1 日游").length).toBeGreaterThan(0);
  expect(plannerStore.getSnapshot().activeVersionId).toBe("ver_1");
});

test("user message exposes separate one-click copy and edit icon actions", async () => {
  const writeText = mockClipboard();
  const createdAt = "2026-06-10T10:11:00+08:00";
  plannerStore.setState({
    agentSession: {
      sessionId: "sess_message_actions",
      status: "active",
      city: "北京",
      title: "北京 AI 行程",
      activePlanId: "plan_message_actions",
      activeVersionId: null,
      turns: [],
      itinerary: null,
      pendingPoiCandidates: []
    },
    conversationTurns: [
      conversationTurn("turn_user_actions", "user", "今年国庆参观北京高校两日游", 1, "active", undefined, createdAt)
    ],
    itineraryPlan: null,
    activeVersionId: null,
    pendingPoiCandidates: []
  });
  vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) {
      return jsonResponse({ mode: "mock", default: [], mock: [] });
    }
    if (path.endsWith("/map/config")) {
      return jsonResponse({ provider: "amap", enabled: true, jsApiKey: "web-key" });
    }
    return jsonResponse({}, 404);
  }));

  render(<AppShell />);

  const copyButton = screen.getByRole("button", { name: "复制消息 1" });
  const editButton = screen.getByRole("button", { name: "编辑消息 1" });
  expect(copyButton.textContent).toBe("");
  expect(editButton.textContent).toBe("");
  expect(screen.getByText("10:11").tagName).toBe("TIME");

  fireEvent.click(copyButton);
  await waitFor(() => expect(writeText).toHaveBeenCalledWith("今年国庆参观北京高校两日游"));
  expect(screen.getByTestId("message-copy-status").textContent).toBe("已复制消息 1");

  writeText.mockRejectedValueOnce(new Error("Clipboard permission denied"));
  fireEvent.click(copyButton);
  await waitFor(() => expect(screen.getByTestId("message-copy-status").textContent).toBe("复制消息 1 失败"));

  fireEvent.click(editButton);
  expect(screen.getByLabelText("编辑消息 1")).toBeTruthy();
});

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" }
  });
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((promiseResolve, promiseReject) => {
    resolve = promiseResolve;
    reject = promiseReject;
  });
  return { promise, resolve, reject };
}

function mockClipboard() {
  const writeText = vi.fn((_text: string) => Promise.resolve());
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText }
  });
  return writeText;
}

function extractionResponse() {
  return {
    inspirationSetId: "insp_error",
    cityCandidates: ["北京"],
    poiCandidates: [{ name: "故宫博物院", confidence: 0.9, sourceLinks: [] }],
    styleTags: [],
    budgetClues: [],
    routeClues: [],
    confidence: 0.8,
    needsUserConfirmation: false,
    sourceLinks: [],
    providerName: "mock-vision-provider",
    fallbackUsed: false,
    itineraryDraft: { title: "北京灵感行程草案", editable: true, days: [] }
  };
}

function itineraryEnvelope() {
  return {
    plan: {
      id: "plan_error",
      title: "北京地图行程草案",
      city: "北京",
      templateType: "custom",
      budgetEstimate: 120,
      budgetDeltaExplanation: "demo",
      decisionRationale: "demo",
      status: "draft",
      days: [],
      routeOptions: [],
      weatherSignals: [],
      trafficCrowdingSignals: [],
      ticketLookupResults: []
    }
  };
}

function itineraryWithDay() {
  return itineraryWithTitle("现有行程");
}

function itineraryWithTitle(title: string) {
  return {
    id: "plan_existing",
    title,
    city: "北京",
    templateType: "agent_mvp",
    budgetEstimate: 60,
    budgetDeltaExplanation: "",
    decisionRationale: "",
    status: "draft",
    days: [
      {
        id: "day_existing",
        dayNumber: 1,
        title: "现有 Day",
        weatherSummary: "",
        riskSummary: "",
        totalEstimatedCost: 60,
        segments: [
          {
            id: "seg_existing",
            startTime: "09:00",
            endTime: "11:00",
            kind: "activity",
            poi: {
              id: "poi_existing",
              name: "故宫博物院",
              city: "北京",
              category: "scenic",
              latitude: 39.918058,
              longitude: 116.397026,
              source: "amap-place-search",
              confidence: 0.91
            },
            transportMode: "walk",
            estimatedCost: 60,
            notes: "现有安排"
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

function conversationTurn(
  id: string,
  role: "user" | "assistant",
  content: string,
  turnIndex: number,
  status: "active" | "superseded" | "failed",
  itineraryVersionId: string | undefined,
  createdAt: string,
  failureReason?: string
) {
  return {
    id,
    role,
    content,
    turnIndex,
    status,
    itineraryVersionId,
    failureReason,
    createdAt,
    updatedAt: createdAt
  };
}

function mapPoi(id: string, name: string) {
  return {
    id,
    name,
    type: "餐饮服务",
    city: "北京市",
    district: "东城区",
    address: "东城",
    longitude: 116.39,
    latitude: 39.91,
    category: "food",
    source: "amap-place-search",
    sourceNote: "高德 WebService POI 搜索",
    confidence: 0.82,
    photos: []
  };
}

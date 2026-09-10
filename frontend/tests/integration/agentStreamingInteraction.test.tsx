import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { AppShell } from "../../src/components/AppShell";
import { InspirationInput } from "../../src/components/agent/InspirationInput";
import type {
  AgentChoiceOption,
  AgentMessageResponse,
  AgentPlanningEvent,
  AgentReasoningStatus,
  AgentSession
} from "../../src/services/apiClient";
import { plannerStore } from "../../src/state/plannerStore";
import {
  createComparisonPreviewState,
  perceptualPlanColorDistance,
  stablePlanColorKey
} from "../../src/state/planComparisonPreview";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  plannerStore.setState({
    selectedCity: "北京",
    activePanel: "agent",
    providerMode: "mock",
    agentSession: null,
    agentSessions: [],
    conversationTurns: [],
    activeVersionId: null,
    itineraryPlan: null,
    planComparison: null,
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
    selectedRouteOptionId: null,
    previewRouteOptionId: null,
    routeWarnings: [],
    itineraryAgentContext: null,
    timelineCopyText: "",
    lastPlanningRun: null,
    comparisonPreview: createComparisonPreviewState()
  });
});

test.each(["plain_link", "share_text", "source_field", "ordinary_link"] as const)(
  "shared source %s uses the Agent request once and preserves the current itinerary",
  async (inputKind) => {
    const link = inputKind === "ordinary_link" ? "https://example.com/guide" : "https://www.xiaohongshu.com/explore/shared-note";
    const content = inputKind === "share_text" ? `这篇北京公园笔记分享给你 ${link}` : link;
    const response = agentMessageResponse();
    const existing = response.itinerary;
    const previous = { ...agentSession(), itinerary: existing, activeVersionId: "ver_existing" };
    const originalText = "北海公园可以步行游览。\n原文中的 <img src=x onerror=alert(1)> 保持为文字。";
    response.assistantTurn = {
      ...response.assistantTurn,
      content: "已读取分享资料的文字正文。",
      planningSteps: [],
      toolEvents: [],
      sharedSource: {
        schemaVersion: "shared-travel-source-v1", status: "completed", sourceMaterialId: "mat_shared",
        canonicalUrl: link, title: "北京公园笔记", bodyText: originalText,
        contentFingerprint: "body-fingerprint", imageCount: 2, imageStatus: "not_read"
      }
    };
    response.userTurn = { ...response.userTurn, content };
    response.version = null;
    response.planningRun = null;
    response.itinerary = existing;
    const pending = deferred<Response>();
    let restored = previous;
    plannerStore.setState({ agentSession: previous, itineraryPlan: existing, activeVersionId: "ver_existing" });
    const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      const path = String(url);
      if (path.endsWith("/providers/status")) return jsonResponse({ mode: "mock", default: [], mock: [] });
      if (path.endsWith("/preferences/extract")) return jsonResponse({ summaryCard: preferenceCard("none") });
      if (path.endsWith("/agent/sessions/current") || path.endsWith("/agent/sessions/sess_stream")) return jsonResponse(restored);
      if (path.endsWith("/agent/sessions")) return jsonResponse({ sessions: [] });
      if (path.endsWith("/messages/stream") && init?.method === "POST") return pending.promise;
      return jsonResponse({}, 404);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<AppShell />);
    fireEvent.change(screen.getByLabelText(inputKind === "source_field" ? "来源链接" : "Agent 对话文本"), {
      target: { value: content }
    });
    fireEvent.click(screen.getByText("发送给 Agent"));
    await waitFor(() => expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/messages/stream"))).toHaveLength(1));
    expect(plannerStore.getSnapshot().itineraryPlan).toEqual(existing);
    expect(plannerStore.getSnapshot().activeVersionId).toBe("ver_existing");
    const request = fetchMock.mock.calls.find(([url]) => String(url).endsWith("/messages/stream"))!;
    const body = JSON.parse(String(request[1]?.body));
    expect(body.content).toBe(content);
    expect(body.requestId).toEqual(expect.any(String));
    expect(body.context.sourceMaterialIds).toBeUndefined();
    expect(fetchMock.mock.calls.some(([url]) => /\/source-materials\/social-link|\/inspirations|\/itineraries\/(generate|compare)/.test(String(url)))).toBe(false);
    pending.resolve(new Response(`${JSON.stringify({ event: "message_response", data: response })}\n`, {
      headers: { "Content-Type": "application/x-ndjson" }
    }));

    const sourceCard = await screen.findByLabelText("分享资料原文");
    expect(within(sourceCard).getByText("检测到 2 张图片，图片尚未解读。")).toBeTruthy();
    expect(within(sourceCard).getByText("只读 · 不改行程")).toBeTruthy();
    expect(sourceCard.querySelector("details")?.hasAttribute("open")).toBe(false);
    fireEvent.click(within(sourceCard).getByText("查看文字正文"));
    expect(sourceCard.textContent).toContain(originalText);
    expect(sourceCard.querySelector("img")).toBeNull();
    expect(within(sourceCard).getByRole("link", { name: "打开来源页面" }).getAttribute("href")).toBe(link);
    expect(plannerStore.getSnapshot().itineraryPlan).toMatchObject(existing!);
    expect(plannerStore.getSnapshot().activeVersionId).toBe("ver_existing");

    restored = { ...previous, turns: [response.userTurn, response.assistantTurn] };
    cleanup();
    plannerStore.setState({ agentSession: null, conversationTurns: [] });
    render(<AppShell />);
    const reloaded = await screen.findByLabelText("分享资料原文");
    expect(reloaded.textContent).toContain(originalText);
    expect(plannerStore.getSnapshot().conversationTurns[1].sharedSource).toMatchObject(response.assistantTurn.sharedSource!);
    expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith("/messages/stream"))).toHaveLength(1);
  }
);

test("mixed URL and file submission is blocked without dropping either input", async () => {
  const onSubmit = vi.fn(async (_payload: import("../../src/services/apiClient").InspirationInputPayload) => true);
  render(<InspirationInput onSubmit={onSubmit} />);
  const link = "https://www.xiaohongshu.com/explore/shared-note";
  fireEvent.change(screen.getByLabelText("Agent 对话文本"), { target: { value: link } });
  fireEvent.drop(screen.getByRole("form", { name: "Agent 对话输入" }), {
    dataTransfer: { files: [new File(["image"], "trip.png", { type: "image/png" })], getData: () => "" }
  });
  fireEvent.click(screen.getByText("发送给 Agent"));
  expect((await screen.findByRole("alert")).textContent).toContain("链接和文件请分开发送");
  expect(onSubmit).not.toHaveBeenCalled();
  expect((screen.getByLabelText("Agent 对话文本") as HTMLTextAreaElement).value).toBe(link);
  expect(screen.getByText("已拖入 1 个文件")).toBeTruthy();
  fireEvent.change(screen.getByLabelText("Agent 对话文本"), { target: { value: "这张截图作为辅助素材" } });
  fireEvent.click(screen.getByText("发送给 Agent"));
  await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
  expect(onSubmit.mock.calls[0][0].files[0].name).toBe("trip.png");
});

test("shared source reload shows unavailable body and unread images without presenting a login page as evidence", async () => {
  const response = agentMessageResponse();
  response.assistantTurn = {
    ...response.assistantTurn, content: "分享链接的公开正文暂时无法读取。", planningSteps: [], toolEvents: [],
    sharedSource: {
      schemaVersion: "shared-travel-source-v1", status: "needs_user_material", sourceMaterialId: "mat_unavailable",
      canonicalUrl: "javascript:alert(1)", title: null, bodyText: "请登录后查看这篇笔记",
      contentFingerprint: null, imageCount: 0, imageStatus: "not_read", failureReason: "login_required"
    }
  };
  const restored = { ...agentSession(), turns: [response.userTurn, response.assistantTurn] };
  vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) return jsonResponse({ mode: "mock", default: [], mock: [] });
    if (path.endsWith("/agent/sessions/current") || path.endsWith("/agent/sessions/sess_stream")) return jsonResponse(restored);
    if (path.endsWith("/agent/sessions")) return jsonResponse({ sessions: [] });
    return jsonResponse({}, 404);
  }));
  render(<AppShell />);
  const card = await screen.findByLabelText("分享资料原文");
  expect(within(card).getByText("未能读取公开正文")).toBeTruthy();
  expect(within(card).getByText("请粘贴分享文字或笔记正文补充资料。")).toBeTruthy();
  expect(within(card).getByText("图片尚未解读。")).toBeTruthy();
  expect(within(card).queryByText("查看文字正文")).toBeNull();
  expect(within(card).queryByText("请登录后查看这篇笔记")).toBeNull();
  expect(within(card).queryByRole("link")).toBeNull();
});

test("pending agent request does not fabricate reasoning before the server emits a real status", async () => {
  const preferenceExtract = deferred<Response>();
  const writeText = mockClipboard();
  let resolveAgentMessage: (response: Response) => void = () => undefined;
  const pendingAgentMessage = new Promise<Response>((resolve) => {
    resolveAgentMessage = resolve;
  });
  const fetchMock = vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) {
      return jsonResponse({ mode: "mock", default: [], mock: [] });
    }
    if (path.endsWith("/preferences/extract")) {
      return preferenceExtract.promise;
    }
    if (path.endsWith("/agent/sessions/current")) {
      return jsonResponse({ detail: "No active conversation session" }, 404);
    }
    if (path.endsWith("/agent/sessions")) {
      return jsonResponse(agentSession());
    }
    if (path.endsWith("/agent/sessions/sess_stream/messages")) {
      return pendingAgentMessage;
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);
  fireEvent.change(screen.getByLabelText("Agent 对话文本"), {
    target: { value: "北京两天，轻松一点，想去故宫和胡同" }
  });
  fireEvent.keyDown(screen.getByLabelText("Agent 对话文本"), { key: "Enter" });

  expect(await screen.findByText("北京两天，轻松一点，想去故宫和胡同")).toBeTruthy();
  expect(screen.queryByLabelText("Agent 规划进度")).toBeNull();
  expect(screen.queryByLabelText("Agent 当前规划进度")).toBeNull();

  preferenceExtract.resolve(jsonResponse({ summaryCard: preferenceCard("用户偏好轻松不赶路。") }));
  await waitFor(() =>
    expect(
      fetchMock.mock.calls.some(([url]) => String(url).endsWith("/agent/sessions/sess_stream/messages"))
    ).toBe(true)
  );
  expect(screen.queryByLabelText("Agent 规划进度")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "复制完整 Agent 调试上下文" }));
  await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
  const pendingCopiedText = String(writeText.mock.calls[0][0]);
  expect(pendingCopiedText).toContain("TRIP_DEBUG_BUNDLE_VERSION=4");
  expect(pendingCopiedText).toContain("北京两天，轻松一点，想去故宫和胡同");
  expect(pendingCopiedText).not.toContain("正在理解行程需求与约束");
  expect(pendingCopiedText).not.toContain("工具输入：");
  expect(pendingCopiedText).not.toContain("返回结果：");
  writeText.mockClear();
  let sawSyntheticAssistantStream = false;
  const streamObserver = new MutationObserver(() => {
    sawSyntheticAssistantStream ||= Boolean(document.querySelector(".chat-row.assistant.streaming"));
  });
  streamObserver.observe(document.body, { childList: true, subtree: true, characterData: true });
  resolveAgentMessage(jsonResponse(agentMessageResponse()));
  await waitFor(() => expect(screen.getAllByText("已按规划过程生成可编辑行程。").length).toBeGreaterThan(0));
  streamObserver.disconnect();
  expect(sawSyntheticAssistantStream).toBe(false);
  await waitFor(() => expect(plannerStore.getSnapshot().preferenceMemory?.memoryText).toContain("轻松不赶路"));
  expect(plannerStore.getSnapshot().preferenceCard?.summaryText).toContain("轻松不赶路");
  await waitFor(() => expect(screen.getByLabelText("Agent turn planning process")).toBeTruthy());
  const turnPanel = screen.getByLabelText("Agent turn planning process");
  expect(turnPanel.tagName).toBe("DETAILS");
  expect(turnPanel.hasAttribute("open")).toBe(false);
  expect(within(turnPanel).getByText("查看详细执行记录")).toBeTruthy();
  expect(screen.getByText("规划方向：3")).toBeTruthy();
  expect(screen.getByText("已展示方案：0")).toBeTruthy();
  expect(screen.getByText("其中部分方案：0")).toBeTruthy();
  expect(screen.getByText("严格验证通过：1")).toBeTruthy();
  expect(screen.getByText("可采用方案：0")).toBeTruthy();
  expect(screen.getByText("联网搜索票务/预约")).toBeTruthy();
  fireEvent.click(
    within(screen.getByLabelText("Agent turn planning process")).getByRole("button", {
      name: "复制查看详细执行记录操作过程"
    })
  );
  await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
  const copiedText = String(writeText.mock.calls[0][0]);
  expect(copiedText).toContain("查看详细执行记录");
  expect(copiedText).toContain("1. 读取当前 itinerary / timeline context");
  expect(copiedText).toContain("2. 高德天气");
  expect(copiedText).not.toContain("工具输入：");
  expect(copiedText).not.toContain("poolReports");
});
test("planning trace copies by default and downloads large or clipboard-denied content within its scope", async () => {
  const writeText = mockClipboard();
  let traceMode: "success" | "large" | "invalid-schema" | "wrong-scope" | "server-error" = "success";
  const response = agentMessageResponse();
  const fetchMock = vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) {
      return jsonResponse({ mode: "mock", default: [], mock: [] });
    }
    if (path.endsWith("/preferences/extract")) {
      return jsonResponse({ summaryCard: preferenceCard("用户偏好轻松不赶路。") });
    }
    if (path.endsWith("/agent/sessions/current")) {
      return jsonResponse({ detail: "No active conversation session" }, 404);
    }
    if (path.endsWith("/agent/sessions")) {
      return jsonResponse(agentSession());
    }
    if (path.endsWith("/agent/sessions/sess_stream/messages")) {
      return jsonResponse(response);
    }
    if (
      path.endsWith("/agent/sessions/sess_stream/turns/turn_stream_assistant/planning-runs/run_stream/trace-export")
    ) {
      if (traceMode === "server-error") {
        return jsonResponse(
          { detail: { code: "planning_trace_scope_mismatch", message: "当前调试详情与规划运行记录不一致。" } },
          409
        );
      }
      return jsonResponse({
        schemaVersion: traceMode === "invalid-schema" ? "trip-planning-trace-v0" : "trip-planning-trace-v1",
        scope: {
          sessionId: "sess_stream",
          assistantTurnId: traceMode === "wrong-scope" ? "turn_other" : "turn_stream_assistant",
          planningRunId: "run_stream"
        },
        phases: [
          {
            phase: "portfolio_candidate_discovery",
            detail: traceMode === "large" ? "大".repeat(400_000) : "",
            webDiscoveryAttempts: [
              {
                query: "北京 高校 官方 地点",
                scope: {
                  briefId: "brief_one",
                  poolId: "pool_one",
                  planningSlotId: "slot_one",
                  dayNumber: 1
                },
                selectedCandidates: [{ amapId: "B0TRACEFIRST", name: "清华大学" }]
              },
              {
                query: "北京 夜景 官方 地点",
                scope: {
                  briefId: "brief_two",
                  poolId: "pool_two",
                  planningSlotId: "slot_two",
                  dayNumber: 2
                },
                selectedCandidates: [{ amapId: "B0TRACELAST", name: "花溪谷" }]
              }
            ]
          }
        ]
      });
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);
  const createObjectUrl = vi.fn((_blob: Blob) => "blob:trace-export");
  const revokeObjectUrl = vi.fn();
  Object.defineProperty(URL, "createObjectURL", { configurable: true, value: createObjectUrl });
  Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: revokeObjectUrl });
  const downloadClick = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);

  render(<AppShell />);
  fireEvent.change(screen.getByLabelText("Agent 对话文本"), {
    target: { value: "北京两天，轻松一点，想去故宫和胡同" }
  });
  fireEvent.keyDown(screen.getByLabelText("Agent 对话文本"), { key: "Enter" });

  await waitFor(() => expect(screen.getByLabelText("Agent turn planning process")).toBeTruthy());
  const planningDetails = screen.getByLabelText("Agent turn planning process") as HTMLDetailsElement;
  planningDetails.open = true;
  fireEvent(planningDetails, new Event("toggle", { bubbles: true }));
  expect(screen.queryByText("调试详情")).toBeNull();
  expect(screen.getByRole("button", { name: "导出完整规划 Trace" })).toBeTruthy();
  expect(fetchMock.mock.calls.some(([url]) => String(url).includes("/trace-export"))).toBe(false);
  const callsBeforeExport = fetchMock.mock.calls.length;
  fireEvent.click(screen.getByRole("button", { name: "导出完整规划 Trace" }));

  await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
  expect(downloadClick).not.toHaveBeenCalled();
  expect(String(writeText.mock.calls[0][0])).toContain("B0TRACEFIRST");
  expect(String(writeText.mock.calls[0][0])).toContain("B0TRACELAST");
  expect(screen.getByText("已复制 Trace")).toBeTruthy();
  const exportCalls = fetchMock.mock.calls.slice(callsBeforeExport).map(([url]) => String(url));
  expect(exportCalls).toEqual([
    expect.stringContaining(
      "/agent/sessions/sess_stream/turns/turn_stream_assistant/planning-runs/run_stream/trace-export"
    )
  ]);
  traceMode = "large";
  fireEvent.click(screen.getByRole("button", { name: "导出完整规划 Trace" }));
  await waitFor(() => expect(downloadClick).toHaveBeenCalledTimes(1));
  expect(writeText).toHaveBeenCalledTimes(1);
  expect(screen.getByText("已下载完整 Trace 文件")).toBeTruthy();
  expect(createObjectUrl).toHaveBeenCalledTimes(1);
  const downloadedBlob = createObjectUrl.mock.calls[0][0] as Blob;
  const downloadedText = await new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result ?? ""));
    reader.onerror = () => reject(reader.error);
    reader.readAsText(downloadedBlob);
  });
  expect(downloadedText).toContain("webDiscoveryAttempts");
  expect(downloadedText).toContain("B0TRACEFIRST");
  expect(downloadedText).toContain("B0TRACELAST");
  expect(downloadedText).not.toContain("TRUNCATED_TO_FIT_500KB_BUNDLE_LIMIT");
  expect(downloadedText).toContain("大".repeat(400_000));
  expect(revokeObjectUrl).toHaveBeenCalledWith("blob:trace-export");
  traceMode = "success";
  writeText.mockRejectedValueOnce(new Error("Clipboard denied"));
  fireEvent.click(screen.getByRole("button", { name: "导出完整规划 Trace" }));
  await waitFor(() => expect(downloadClick).toHaveBeenCalledTimes(2));
  traceMode = "invalid-schema";
  fireEvent.click(screen.getByRole("button", { name: "导出完整规划 Trace" }));
  await waitFor(() => expect(screen.getByText("规划 Trace 响应格式不受支持。")).toBeTruthy());
  expect(downloadClick).toHaveBeenCalledTimes(2);
  traceMode = "wrong-scope";
  fireEvent.click(screen.getByRole("button", { name: "导出完整规划 Trace" }));
  await waitFor(() => expect(screen.getByText("规划 Trace 响应范围与请求不一致。")).toBeTruthy());
  expect(downloadClick).toHaveBeenCalledTimes(2);
  traceMode = "server-error";
  fireEvent.click(screen.getByRole("button", { name: "导出完整规划 Trace" }));
  await waitFor(() => expect(screen.getByText("当前调试详情与规划运行记录不一致。")).toBeTruthy());
  expect(plannerStore.getSnapshot().activeVersionId).toBe("ver_stream");
});
test("staged diagnostic planning events render nested and top-level result previews", async () => {
  const writeText = mockClipboard();
  const fetchMock = vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) {
      return jsonResponse({ mode: "mock", default: [], mock: [] });
    }
    if (path.endsWith("/preferences/extract")) {
      return jsonResponse({ summaryCard: preferenceCard("用户偏好轻松不赶路。") });
    }
    if (path.endsWith("/agent/sessions/current")) {
      return jsonResponse({ detail: "No active conversation session" }, 404);
    }
    if (path.endsWith("/agent/sessions")) {
      return jsonResponse(agentSession());
    }
    if (path.endsWith("/agent/sessions/sess_stream/messages/stream")) {
      return jsonResponse({}, 404);
    }
    if (path.endsWith("/agent/sessions/sess_stream/messages")) {
      return jsonResponse(agentMessageResponse());
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);
  fireEvent.change(screen.getByLabelText("Agent 对话文本"), {
    target: { value: "北京高校两日游，晚上看北京夜景" }
  });
  fireEvent.keyDown(screen.getByLabelText("Agent 对话文本"), { key: "Enter" });

  await waitFor(() => expect(screen.getAllByText("已按规划过程生成可编辑行程。").length).toBeGreaterThan(0));
  const turnPanel = screen.getByLabelText("Agent turn planning process");
  within(turnPanel)
    .getAllByText("查看工具详情")
    .forEach((summary) => fireEvent.click(summary));

  expect(within(turnPanel).getAllByText("返回结果").length).toBeGreaterThanOrEqual(3);
  expect(within(turnPanel).getAllByText(/poolReports/).length).toBeGreaterThanOrEqual(2);
  expect(within(turnPanel).getByText(/夜景观景点/)).toBeTruthy();
  expect(within(turnPanel).getByText(/dayReadiness/)).toBeTruthy();
  expect(within(turnPanel).getByText(/versionCreated/)).toBeTruthy();

  fireEvent.click(within(turnPanel).getByRole("button", { name: "复制查看详细执行记录操作过程" }));
  await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
  const copiedText = String(writeText.mock.calls[0][0]);
  expect(copiedText).not.toContain("poolReports");
  expect(copiedText).not.toContain("dayReadiness");
});
test("action trace shows public goal input result decision and effect without rendering internal steps", async () => {
  const writeText = mockClipboard();
  const response = agentMessageResponse();
  const events: AgentPlanningEvent[] = [
    {
      ...planningEventsFixture()[0],
      type: "context",
      label: "读取当前 itinerary / timeline context",
      userVisible: false,
      category: "internal"
    },
    {
      ...planningEventsFixture()[1],
      type: "resolve_poi",
      label: "resolve_poi",
      actionLabel: "核验地图地点",
      userVisible: true,
      sequence: 1,
      category: "tool_action",
      goal: "替换第一天午餐为人大食堂",
      inputSummary: "query=中国人民大学食堂 · city=北京 · category=food",
      resultSummary: "resolvedCount=0 · pendingCount=1 · candidateCount=3",
      decisionSummary: "候选存在歧义，等待用户选择",
      effectSummary: "本轮未修改时间轴；activeVersionId 保持不变"
    },
    {
      ...planningEventsFixture()[2],
      type: "agent_run",
      label: "Agent 规划耗时",
      userVisible: false,
      category: "internal",
      metadata: {
        outcomeStatuses: {
          itineraryStatus: "needs_confirmation",
          mapStatus: "pending",
          routeStatus: "not_applicable",
          reservationStatus: "not_checked",
          sourceQualityStatus: "pending"
        }
      }
    }
  ];
  response.planningSteps = events;
  response.toolEvents = events;
  response.assistantTurn.planningSteps = events;
  response.assistantTurn.toolEvents = events;
  const fetchMock = vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) return jsonResponse({ mode: "mock", default: [], mock: [] });
    if (path.endsWith("/preferences/extract")) return jsonResponse({ summaryCard: preferenceCard("暂无") });
    if (path.endsWith("/agent/sessions/current"))
      return jsonResponse({ detail: "No active conversation session" }, 404);
    if (path.endsWith("/agent/sessions")) return jsonResponse(agentSession());
    if (path.endsWith("/agent/sessions/sess_stream/messages/stream")) return jsonResponse({}, 404);
    if (path.endsWith("/agent/sessions/sess_stream/messages")) return jsonResponse(response);
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);
  fireEvent.change(screen.getByLabelText("Agent 对话文本"), { target: { value: "第一天中午去人大的食堂吃吧" } });
  fireEvent.keyDown(screen.getByLabelText("Agent 对话文本"), { key: "Enter" });

  await waitFor(() => expect(screen.getByLabelText("Agent 可见动作轨迹")).toBeTruthy());
  const trace = screen.getByLabelText("Agent 可见动作轨迹");
  expect(within(trace).getByText("核验地图地点")).toBeTruthy();
  expect(within(trace).getByText("目标：替换第一天午餐为人大食堂")).toBeTruthy();
  expect(within(trace).getByText(/输入：query=中国人民大学食堂/)).toBeTruthy();
  expect(within(trace).getByText(/结果：resolvedCount=0/)).toBeTruthy();
  expect(within(trace).getByText("决定：候选存在歧义，等待用户选择")).toBeTruthy();
  expect(within(trace).getByText(/影响：本轮未修改时间轴/)).toBeTruthy();
  expect(screen.queryByText(/技术详情/)).toBeNull();
  expect(screen.queryByText("读取当前 itinerary / timeline context")).toBeNull();
  expect(screen.queryByLabelText("Agent 结果维度")).toBeNull();

  fireEvent.click(screen.getByRole("button", { name: "复制查看详细执行记录操作过程" }));
  await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
  const copied = String(writeText.mock.calls[0][0]);
  expect(copied).toContain("核验地图地点");
  expect(copied).not.toContain("读取当前 itinerary / timeline context");
});
test("partial timeline response renders selected segments and unresolved planning metadata", async () => {
  const fetchMock = vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) {
      return jsonResponse({ mode: "mock", default: [], mock: [] });
    }
    if (path.endsWith("/preferences/extract")) {
      return jsonResponse({ summaryCard: preferenceCard("用户偏好公交地铁优先。") });
    }
    if (path.endsWith("/agent/sessions/current")) {
      return jsonResponse({ detail: "No active conversation session" }, 404);
    }
    if (path.endsWith("/agent/sessions")) {
      return jsonResponse(agentSession());
    }
    if (path.endsWith("/agent/sessions/sess_stream/messages/stream")) {
      return jsonResponse({}, 404);
    }
    if (path.endsWith("/agent/sessions/sess_stream/messages")) {
      return jsonResponse(partialTimelineAgentMessageResponse());
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);
  fireEvent.change(screen.getByLabelText("Agent 对话文本"), {
    target: { value: "北京峡谷漂流两日游，午餐晚餐体验北京美食" }
  });
  fireEvent.keyDown(screen.getByLabelText("Agent 对话文本"), { key: "Enter" });

  await waitFor(() => expect(screen.getAllByText(/已生成可编辑草案/).length).toBeGreaterThan(0));
  await waitFor(() => expect(plannerStore.getSnapshot().itineraryPlan?.title).toBe("北京峡谷漂流部分草案"));
  expect(plannerStore.getSnapshot().selectedDayNumber).toBe(2);
  expect(screen.getAllByTestId("timeline-segment-name").some((node) => node.textContent === "近郊漂流景区")).toBe(true);
  expect(screen.getAllByText(/部分主题地点因绕路风险/).length).toBeGreaterThan(0);

  const turnPanel = screen.getByLabelText("Agent turn planning process");
  within(turnPanel)
    .getAllByText("查看工具详情")
    .forEach((summary) => fireEvent.click(summary));
  expect(within(turnPanel).getByText(/route_detour_penalty/)).toBeTruthy();
  expect(within(turnPanel).getByText(/persist_viable_partial_days/)).toBeTruthy();

  const messageCallsBeforeMap = fetchMock.mock.calls.filter(([url]) => String(url).includes("/messages")).length;
  fireEvent.click(screen.getByRole("button", { name: "打开 Day 1 14:00-16:00 待补地点候选" }));
  await waitFor(() => expect(screen.getByRole("tab", { name: "地图" }).getAttribute("aria-selected")).toBe("true"));
  expect(plannerStore.getSnapshot().activeDensityMapComparison).toMatchObject({
    briefId: "brief_partial",
    poolId: "pool_partial_1",
    planningSlotId: "slot_partial_1",
    dayNumber: 1
  });
  await waitFor(() =>
    expect((document.activeElement as HTMLButtonElement | null)?.textContent).toContain("在地图对比 Day 1 候选位置")
  );
  expect(fetchMock.mock.calls.filter(([url]) => String(url).includes("/messages")).length).toBe(messageCallsBeforeMap);
});

test("test bundle copy button is disabled when empty and hidden when the agent panel is collapsed", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: RequestInfo | URL) => {
      const path = String(url);
      if (path.endsWith("/providers/status")) {
        return jsonResponse({ mode: "mock", default: [], mock: [] });
      }
      if (path.endsWith("/agent/sessions/current")) {
        return jsonResponse({ detail: "No active conversation session" }, 404);
      }
      return jsonResponse({}, 404);
    })
  );

  render(<AppShell />);

  const copyButton = screen.getByRole("button", { name: "复制完整 Agent 调试上下文" });
  expect((copyButton as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "收缩左侧对话" }));
  await waitFor(() => expect(screen.queryByRole("button", { name: "复制完整 Agent 调试上下文" })).toBeNull());
});

test("copies one versioned test bundle with conversation and redacted debug sections", async () => {
  const writeText = mockClipboard();
  const fetchMock = vi.fn(async (url: RequestInfo | URL, _init?: RequestInit) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) {
      return jsonResponse({ mode: "mock", default: [], mock: [] });
    }
    if (path.endsWith("/preferences/extract")) {
      return jsonResponse({ summaryCard: preferenceCard("用户偏好轻松不赶路。") });
    }
    if (path.endsWith("/agent/sessions/current")) {
      return jsonResponse({ detail: "No active conversation session" }, 404);
    }
    if (path.endsWith("/agent/sessions")) {
      return jsonResponse(agentSession());
    }
    if (path.endsWith("/agent/sessions/sess_stream/messages/stream")) {
      return jsonResponse({}, 404);
    }
    if (path.endsWith("/agent/sessions/sess_stream/messages")) {
      return jsonResponse(agentMessageResponse());
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);
  fireEvent.change(screen.getByLabelText("Agent 对话文本"), {
    target: { value: "对话\n复制测试信息\n‹\n北京两天，轻松一点，想去故宫和胡同" }
  });
  fireEvent.click(screen.getByText("发送给 Agent"));

  await waitFor(() => expect(screen.getAllByText("已按规划过程生成可编辑行程。").length).toBeGreaterThan(0));
  const messageRequest = fetchMock.mock.calls.find(([url]) =>
    String(url).endsWith("/agent/sessions/sess_stream/messages")
  );
  expect(messageRequest).toBeTruthy();
  const requestBody = JSON.parse(String((messageRequest?.[1] as RequestInit | undefined)?.body));
  expect(requestBody.content).toBe("北京两天，轻松一点，想去故宫和胡同");
  expect(requestBody.context.currentUserMessage).toBe("北京两天，轻松一点，想去故宫和胡同");
  fireEvent.click(screen.getByRole("button", { name: "复制完整 Agent 调试上下文" }));
  await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "复制完整 Agent 调试上下文" }).textContent).toBe("完整上下文已复制")
  );
  const copiedText = String(writeText.mock.calls[0][0]);
  expect(copiedText).toContain("TRIP_DEBUG_BUNDLE_VERSION=4");
  expect(copiedText).toContain("=== CONVERSATION_TURNS ===");
  expect(copiedText).toContain("北京两天，轻松一点，想去故宫和胡同");
  expect(copiedText).toContain("已按规划过程生成可编辑行程。");
  expect(copiedText).toContain("END_TRIP_DEBUG_BUNDLE");
  expect(copiedText).toContain("=== PLANNING_TRACE ===");
  expect(copiedText).toContain("=== PROPOSAL_VERIFIERS ===");
  expect(copiedText).toContain("=== ACTIVE_ITINERARY ===");
});

test("semantic candidate shortage no-version response shows hint retry message", async () => {
  const fetchMock = vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) {
      return jsonResponse({ mode: "mock", default: [], mock: [] });
    }
    if (path.endsWith("/preferences/extract")) {
      return jsonResponse({ summaryCard: preferenceCard("用户偏好轻松不赶路。") });
    }
    if (path.endsWith("/agent/sessions/current")) {
      return jsonResponse({ detail: "No active conversation session" }, 404);
    }
    if (path.endsWith("/agent/sessions")) {
      return jsonResponse(agentSession());
    }
    if (path.endsWith("/agent/sessions/sess_stream/messages/stream")) {
      return jsonResponse({}, 404);
    }
    if (path.endsWith("/agent/sessions/sess_stream/messages")) {
      const response: any = agentMessageResponse();
      response.version = null;
      response.itinerary = null;
      response.warnings = ["缺少候选语义提示，尚未创建正式时间轴。"];
      response.assistantTurn.content = "缺少候选语义提示，尚未创建正式时间轴。";
      response.assistantTurn.itineraryVersionId = null;
      return jsonResponse(response);
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);
  fireEvent.change(screen.getByLabelText("Agent 对话文本"), {
    target: { value: "北京高校两日游，晚上看北京夜景" }
  });
  fireEvent.keyDown(screen.getByLabelText("Agent 对话文本"), { key: "Enter" });

  await waitFor(() =>
    expect(
      screen
        .getAllByRole("alert")
        .some((alert) => alert.textContent?.includes("规划预览已生成，但尚未创建正式时间轴。缺少候选语义提示。"))
    ).toBe(true)
  );
  const agentAlert = screen.getAllByRole("alert").find((alert) => alert.textContent?.includes("缺少候选语义提示"));
  expect(agentAlert?.textContent).toContain("规划预览已生成");
  expect(screen.getByRole("button", { name: "让 Agent 自动补充候选" })).not.toBeNull();
  expect(screen.getByRole("button", { name: "我来输入候选" })).not.toBeNull();
  expect(screen.queryByText("地图服务限流或候选未完成，暂未生成可执行行程。可稍后重试。")).toBeNull();
});

test("provider rate limited no-version response shows map rate limit message", async () => {
  const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) {
      return jsonResponse({ mode: "mock", default: [], mock: [] });
    }
    if (path.endsWith("/preferences/extract")) {
      return jsonResponse({ summaryCard: preferenceCard("用户偏好轻松不赶路。") });
    }
    if (path.endsWith("/agent/sessions/current")) {
      return jsonResponse({ detail: "No active conversation session" }, 404);
    }
    if (path.endsWith("/agent/sessions")) {
      return jsonResponse(agentSession());
    }
    if (path.endsWith("/agent/sessions/sess_stream/messages/stream")) {
      return jsonResponse({}, 404);
    }
    if (path.endsWith("/agent/sessions/sess_stream/messages")) {
      const response: any = agentMessageResponse();
      response.version = null;
      response.itinerary = null;
      response.warnings = ["地图服务暂时限流，已保留本轮需求和规划预览。稍后点击继续即可重试，不需要重新说明。"];
      response.assistantTurn.content =
        "地图服务暂时限流，本轮没有写入正式时间轴。我已保留这次完整旅行需求和规划预览；稍后点击继续重试即可从上次进度继续，不需要重新说明。";
      response.assistantTurn.itineraryVersionId = null;
      return jsonResponse(response);
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);
  fireEvent.change(screen.getByLabelText("Agent 对话文本"), {
    target: { value: "北京高校两日游，晚上看北京夜景" }
  });
  fireEvent.keyDown(screen.getByLabelText("Agent 对话文本"), { key: "Enter" });

  await waitFor(() =>
    expect(screen.getAllByRole("alert").some((alert) => alert.textContent?.includes("已保留本轮需求和规划预览"))).toBe(
      true
    )
  );
  expect(screen.getByRole("button", { name: "继续" })).not.toBeNull();
  expect(screen.queryByRole("button", { name: "让 Agent 自动补充候选" })).toBeNull();
  expect(screen.queryByText("地图服务限流或候选未完成，暂未生成可执行行程。可稍后重试。")).toBeNull();
});

test("old no-version retry affordance is hidden after an active version exists", async () => {
  const createdAt = "2026-06-10T10:00:00Z";
  const session = {
    ...agentSession(),
    activeVersionId: "ver_existing",
    turns: [
      {
        id: "turn_old_user",
        role: "user",
        content: "北京高校两日游，晚上看夜景",
        turnIndex: 1,
        status: "active",
        createdAt,
        updatedAt: createdAt
      },
      {
        id: "turn_old_waiting",
        role: "assistant",
        content: "地图服务暂时限流，本轮没有写入正式时间轴。我已保留可续跑草案。",
        turnIndex: 2,
        status: "active",
        itineraryVersionId: null,
        planningSteps: [
          {
            type: "collect_candidates",
            label: "检索高德候选",
            status: "fallback",
            detail: "地图服务限流，待重试。",
            providerName: "agent-staged-pipeline",
            fallbackUsed: true,
            metadata: {
              resultPreview: {
                resultState: "provider_rate_limited",
                nextActions: ["retry_after_map_provider_recovers"]
              }
            },
            timestamp: createdAt
          }
        ],
        toolEvents: [],
        createdAt,
        updatedAt: createdAt
      }
    ],
    itinerary: {
      id: "plan_stream",
      title: "已有时间轴",
      city: "北京",
      templateType: "custom",
      budgetTarget: 3000,
      budgetEstimate: 0,
      budgetDeltaExplanation: "",
      decisionRationale: "",
      status: "draft",
      days: [],
      routeOptions: [],
      weatherSignals: [],
      trafficCrowdingSignals: [],
      ticketLookupResults: []
    }
  };
  const fetchMock = vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) {
      return jsonResponse({ mode: "mock", default: [], mock: [] });
    }
    if (path.endsWith("/agent/sessions/current")) {
      return jsonResponse(session);
    }
    if (path.endsWith("/agent/sessions")) {
      return jsonResponse({ sessions: [] });
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);

  await waitFor(() => expect(screen.getByText(/地图服务暂时限流/)).not.toBeNull());
  expect(screen.queryByRole("button", { name: "继续" })).toBeNull();
});

test("raw execution events stay in the folded trace while server reasoning drives the live status", async () => {
  const finishStream = deferred<void>();
  const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) {
      return jsonResponse({ mode: "mock", default: [], mock: [] });
    }
    if (path.endsWith("/preferences/extract")) {
      return jsonResponse({ summaryCard: preferenceCard("用户偏好轻松不赶路。") });
    }
    if (path.endsWith("/agent/sessions/current")) {
      return jsonResponse({ detail: "No active conversation session" }, 404);
    }
    if (path.endsWith("/agent/sessions")) {
      return jsonResponse(agentSession());
    }
    if (path.endsWith("/agent/sessions/sess_stream/messages/stream") && init?.method === "POST") {
      return agentExecutionStreamResponse(finishStream.promise);
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);
  fireEvent.change(screen.getByLabelText("Agent 对话文本"), {
    target: { value: "北京两天，轻松一点，想去故宫和胡同" }
  });
  fireEvent.keyDown(screen.getByLabelText("Agent 对话文本"), { key: "Enter" });

  await waitFor(() => expect(screen.getByText("正在读取当前行程并检查可修改范围")).toBeTruthy());
  const progress = screen.getByLabelText("Agent 规划进度");
  expect(progress.getAttribute("data-message-type")).toBe("reasoning_status");
  expect(within(progress).getByText("正在读取当前行程并检查可修改范围")).toBeTruthy();
  expect(screen.queryByText(/第 \d+ 步/)).toBeNull();
  expect(within(progress).queryByText("read_itinerary")).toBeNull();
  expect(within(progress).queryByText("执行 Agent 工具循环")).toBeNull();
  expect(within(progress).queryByText("Agent 执行中")).toBeNull();
  expect(within(progress).queryByText("后端仍在等待当前安全阶段完成。")).toBeNull();
  expect(screen.queryByText("发送中")).toBeNull();
  expect(screen.getByRole("button", { name: "编辑消息 1" })).toBeTruthy();
  expect(
    fetchMock.mock.calls.some((call) => String(call[0]).endsWith("/agent/sessions/sess_stream/messages/stream"))
  ).toBe(true);
  expect(fetchMock.mock.calls.some((call) => String(call[0]).endsWith("/agent/sessions/sess_stream/messages"))).toBe(
    false
  );

  finishStream.resolve();
  await waitFor(() => expect(screen.getAllByText("已按规划过程生成可编辑行程。").length).toBeGreaterThan(0));
  await waitFor(() => expect(screen.getByLabelText("Agent turn planning process")).toBeTruthy());
  expect(screen.getByText("查看详细执行记录")).toBeTruthy();
  expect(screen.getByText("patch_itinerary")).toBeTruthy();
});

test("first committed safe partial enters comparison without auto-submitting more plans", async () => {
  const response = agentMessageResponse();
  const projection = {
    planningSelectionRootTurnId: "root_safe_partial",
    rootPortfolioId: "partial:root_safe_partial",
    proposalId: "partial:partial:root_safe_partial",
    sourceAssistantTurnId: response.assistantTurn.id,
    choiceId: "portfolio_partial_adopt_partial_root_safe_partial_ver_stream",
    status: "partial",
    isPartial: true,
    isAdopted: false,
    adoptionReady: true,
    activeVersionId: "ver_stream",
    expectedBaseVersionId: "ver_stream",
    title: "规则安全草稿（部分完成）",
    days: [],
    pendingSlots: [
      {
        briefId: "partial_brief",
        poolId: "meal_pool",
        planningSlotId: "day1_lunch",
        dayNumber: 1,
        timeWindow: "12:00-13:00",
        displayNeed: "午餐"
      },
      {
        briefId: "partial_brief",
        poolId: "meal_pool",
        planningSlotId: "day2_lunch",
        dayNumber: 2,
        timeWindow: "12:00-13:00",
        displayNeed: "午餐"
      }
    ],
    routeEvidence: [],
    budgetSummary: "中等预算",
    routeSummary: "已验证真实地点",
    tradeoffSummary: "仍有 2 个时段待补",
    colorKey: "plan-color-0"
  };
  response.assistantTurn.content = "已生成可见的部分时间轴；当前还有 2 个待补时段。";
  response.assistantTurn.choiceOptions = [
    {
      id: projection.choiceId,
      action: "adopt_active_partial",
      kind: "plan_proposal",
      label: "采用当前部分方案",
      comparisonProjection: projection
    },
    {
      id: "portfolio_partial_more_plans_root_safe_partial_ver_stream",
      action: "retry_model_planning",
      kind: "portfolio_partial_more_plans",
      label: "继续生成其他方案",
      expectedBaseVersionId: "ver_stream",
      planningSelectionRootTurnId: "root_safe_partial",
      rootPortfolioId: "partial:root_safe_partial",
      focusBriefId: "partial_brief",
      requestContractFingerprint: "f".repeat(64),
      lifecycle: "offered"
    }
  ];
  const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) return jsonResponse({ mode: "mock", default: [], mock: [] });
    if (path.endsWith("/preferences/extract")) return jsonResponse({ summaryCard: preferenceCard("暂无") });
    if (path.endsWith("/agent/sessions/current"))
      return jsonResponse({ detail: "No active conversation session" }, 404);
    if (path.endsWith("/agent/sessions")) return jsonResponse(agentSession());
    if (path.endsWith("/agent/sessions/sess_stream/messages/stream") && init?.method === "POST") {
      return comparisonProjectionStreamResponse(response, projection);
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);
  fireEvent.change(screen.getByLabelText("Agent 对话文本"), { target: { value: "使用规则安全草稿" } });
  fireEvent.keyDown(screen.getByLabelText("Agent 对话文本"), { key: "Enter" });

  await waitFor(() => expect(plannerStore.getSnapshot().comparisonPreview.plans).toHaveLength(1));
  await new Promise((resolve) => window.setTimeout(resolve, 20));
  const initialStreamCalls = fetchMock.mock.calls.filter((call) =>
    String(call[0]).endsWith("/agent/sessions/sess_stream/messages/stream")
  );
  expect(initialStreamCalls).toHaveLength(1);
  expect(
    plannerStore
      .getSnapshot()
      .conversationTurns.some((turn) => turn.role === "user" && turn.content === "继续生成其他方案")
  ).toBe(false);

  fireEvent.click(screen.getByRole("button", { name: /继续生成其他方案/ }));
  await waitFor(() => {
    const streamCalls = fetchMock.mock.calls.filter((call) =>
      String(call[0]).endsWith("/agent/sessions/sess_stream/messages/stream")
    );
    expect(streamCalls).toHaveLength(2);
    const explicitExpansionBody = JSON.parse(String(streamCalls[1][1]?.body));
    expect(explicitExpansionBody.context.selectedAgentChoice).toEqual({
      sourceAssistantTurnId: response.assistantTurn.id,
      choiceId: "portfolio_partial_more_plans_root_safe_partial_ver_stream"
    });
  });
  expect(plannerStore.getSnapshot().comparisonPreview.autoNavigationCount).toBe(1);
  expect(plannerStore.getSnapshot().comparisonPreview.plans[0].pendingSlots).toHaveLength(2);
  await waitFor(() => expect(screen.getAllByText("规则安全草稿（部分完成）").length).toBeGreaterThan(0));
  expect(plannerStore.getSnapshot().comparisonPreview.autoNavigationCount).toBe(1);
  console.log(
    `TRIP_RULE_SAFE_COMPARISON_FRONTEND_METRICS=${JSON.stringify({
      comparisonAutoNavigated: true,
      autoNavigationCount: plannerStore.getSnapshot().comparisonPreview.autoNavigationCount,
      projectedPlanCount: plannerStore.getSnapshot().comparisonPreview.plans.length,
      pendingSlotCount: plannerStore.getSnapshot().comparisonPreview.plans[0].pendingSlots.length
    })}`
  );
});

test("portfolio visible stream appends A to B to C with one navigation and opaque continuation choices", async () => {
  const projections = [
    comparisonStreamProjection("proposal_a", "Plan A"),
    comparisonStreamProjection("proposal_b", "Plan B"),
    comparisonStreamProjection("proposal_c", "Plan C")
  ];
  const responses = projections.map((projection, index) => {
    const response = agentMessageResponse();
    response.userTurn = { ...response.userTurn, id: `turn_stream_user_${index + 1}`, content: `request ${index + 1}` };
    response.assistantTurn = {
      ...response.assistantTurn,
      id: `turn_stream_assistant_${index + 1}`,
      content: `generated ${projection.title}`,
      comparisonProjectionUpdateMode: index === 0 ? "replace" : "append",
      structuredChoiceTrace:
        index === 0
          ? null
          : {
              sourceAssistantTurnId: `turn_stream_assistant_${index}`,
              sourceAssistantTurnRole: "assistant",
              sourceAssistantTurnStatus: "active",
              requestChoiceId: `continue_${index}`,
              persistedChoiceId: `continue_${index}`,
              resolvedChoiceId: `continue_${index}`,
              executionChoiceId: `continue_${index}`,
              planningSelectionRootTurnId: projection.planningSelectionRootTurnId,
              rootPortfolioId: projection.rootPortfolioId,
              executionAction: "retry_model_planning",
              executionRoute: "controller_choice_resume",
              executionStatus: "succeeded",
              outcome: {
                reason: "new_verified_proposal",
                versionDelta: 0,
                patchDelta: 0,
                routeWriteDelta: 0
              }
            },
      comparisonProjections: [{ ...projection, sourceAssistantTurnId: `turn_stream_assistant_${index + 1}` }],
      choiceOptions: [
        {
          id: projection.choiceId,
          action: "select_plan_proposal",
          kind: "plan_proposal",
          label: projection.title,
          comparisonProjection: { ...projection, sourceAssistantTurnId: `turn_stream_assistant_${index + 1}` }
        },
        ...(index < projections.length - 1
          ? [
              {
                id: `continue_${index + 1}`,
                action: "retry_model_planning" as const,
                kind: "portfolio_partial_more_plans" as const,
                label: `Continue ${projections[index + 1].title}`,
                lifecycle: "offered" as const
              }
            ]
          : [])
      ]
    };
    return response;
  });
  const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) return jsonResponse({ mode: "mock", default: [], mock: [] });
    if (path.endsWith("/preferences/extract")) return jsonResponse({ summaryCard: preferenceCard("none") });
    if (path.endsWith("/agent/sessions/current"))
      return jsonResponse({ detail: "No active conversation session" }, 404);
    if (path.endsWith("/agent/sessions")) return jsonResponse(agentSession());
    if (path.endsWith("/agent/sessions/sess_stream/messages/stream") && init?.method === "POST") {
      const streamCalls = fetchMock.mock.calls.filter((call) =>
        String(call[0]).endsWith("/agent/sessions/sess_stream/messages/stream")
      );
      const index = streamCalls.length - 1;
      return comparisonProjectionStreamResponse(responses[index], {
        ...projections[index],
        sourceAssistantTurnId: responses[index].assistantTurn.id
      });
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);
  fireEvent.change(screen.getAllByRole("textbox")[0], { target: { value: "initial request" } });
  fireEvent.keyDown(screen.getAllByRole("textbox")[0], { key: "Enter" });

  await waitFor(() => expect(plannerStore.getSnapshot().comparisonPreview.plans).toHaveLength(1));
  expect(plannerStore.getSnapshot().comparisonPreview.plans.map((plan) => plan.proposalId)).toEqual(["proposal_a"]);
  expect(plannerStore.getSnapshot().comparisonPreview.focusedProposalId).toBe("proposal_a");
  expect(plannerStore.getSnapshot().comparisonPreview.autoNavigationCount).toBe(1);
  expect(plannerStore.getSnapshot().conversationTurns.filter((turn) => turn.role === "user")).toHaveLength(1);

  fireEvent.click(screen.getByRole("button", { name: /Continue Plan B/ }));
  await waitFor(() => expect(plannerStore.getSnapshot().comparisonPreview.plans).toHaveLength(2));
  const afterB = plannerStore.getSnapshot().comparisonPreview;
  expect(afterB.plans.map((plan) => plan.proposalId)).toEqual(["proposal_a", "proposal_b"]);
  expect(afterB.focusedProposalId).toBe("proposal_a");
  expect(afterB.autoNavigationCount).toBe(1);
  const afterBColors = afterB.plans.map((plan) => plan.colorKey);
  expect(afterBColors[0]).toBe(stablePlanColorKey("proposal_a"));
  expect(new Set(afterBColors).size).toBe(2);
  expect(perceptualPlanColorDistance(afterBColors[0], afterBColors[1])).toBeGreaterThanOrEqual(90);

  fireEvent.click(screen.getByRole("button", { name: /Continue Plan C/ }));
  await waitFor(() => expect(plannerStore.getSnapshot().comparisonPreview.plans).toHaveLength(3));
  const afterC = plannerStore.getSnapshot().comparisonPreview;
  expect(afterC.plans.map((plan) => plan.proposalId)).toEqual(["proposal_a", "proposal_b", "proposal_c"]);
  expect(afterC.focusedProposalId).toBe("proposal_a");
  expect(afterC.autoNavigationCount).toBe(1);
  const afterCColors = afterC.plans.map((plan) => plan.colorKey);
  expect(afterCColors.slice(0, 2)).toEqual(afterBColors);
  expect(new Set(afterCColors).size).toBe(3);

  const streamCalls = fetchMock.mock.calls.filter((call) =>
    String(call[0]).endsWith("/agent/sessions/sess_stream/messages/stream")
  );
  expect(streamCalls).toHaveLength(3);
  expect(JSON.parse(String(streamCalls[1][1]?.body)).context.selectedAgentChoice).toEqual({
    sourceAssistantTurnId: "turn_stream_assistant_1",
    choiceId: "continue_1"
  });
  expect(JSON.parse(String(streamCalls[2][1]?.body)).context.selectedAgentChoice).toEqual({
    sourceAssistantTurnId: "turn_stream_assistant_2",
    choiceId: "continue_2"
  });
  expect(JSON.parse(String(streamCalls[1][1]?.body)).context.selectedAgentChoice).not.toHaveProperty("manualValue");
  expect(JSON.parse(String(streamCalls[2][1]?.body)).context.selectedAgentChoice).not.toHaveProperty("manualValue");
  expect(fetchMock.mock.calls.some((call) => String(call[0]).endsWith("/agent/sessions/sess_stream/messages"))).toBe(
    false
  );
  expect(plannerStore.getSnapshot().conversationTurns.filter((turn) => turn.role === "user")).toHaveLength(3);
  const finalPreview = plannerStore.getSnapshot().comparisonPreview;
  console.log(
    `TRIP_RULE_SAFE_COMPARISON_FRONTEND_METRICS=${JSON.stringify({
      comparisonAutoNavigated: true,
      autoNavigationCount: finalPreview.autoNavigationCount,
      projectedPlanCount: finalPreview.plans.length,
      distinctProposalCount: new Set(finalPreview.plans.map((plan) => plan.proposalId)).size,
      stableColorMapping: new Set(finalPreview.plans.map((plan) => plan.colorKey)).size === finalPreview.plans.length,
      rootPortfolioDriftCount: new Set(finalPreview.plans.map((plan) => plan.rootPortfolioId)).size - 1
    })}`
  );
});

test("a later portfolio stream failure preserves already visible cards, focus, colors, and navigation state", async () => {
  const first = comparisonStreamProjection("proposal_a", "Plan A");
  const second = comparisonStreamProjection("proposal_b", "Plan B");
  const initialResponse = agentMessageResponse();
  initialResponse.assistantTurn = {
    ...initialResponse.assistantTurn,
    choiceOptions: [
      {
        id: first.choiceId,
        action: "select_plan_proposal",
        kind: "plan_proposal",
        label: first.title,
        comparisonProjection: { ...first, sourceAssistantTurnId: initialResponse.assistantTurn.id }
      },
      {
        id: "continue_b",
        action: "retry_model_planning",
        kind: "portfolio_partial_more_plans",
        label: "Continue Plan B",
        lifecycle: "offered"
      }
    ]
  };
  const failedResponse = agentMessageResponse();
  failedResponse.userTurn = { ...failedResponse.userTurn, id: "turn_stream_user_2", content: "Continue Plan B" };
  failedResponse.assistantTurn = { ...failedResponse.assistantTurn, id: "turn_stream_assistant_2" };
  let streamCallCount = 0;
  const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) return jsonResponse({ mode: "mock", default: [], mock: [] });
    if (path.endsWith("/preferences/extract")) return jsonResponse({ summaryCard: preferenceCard("none") });
    if (path.endsWith("/agent/sessions/current"))
      return jsonResponse({ detail: "No active conversation session" }, 404);
    if (path.endsWith("/agent/sessions")) return jsonResponse(agentSession());
    if (path.endsWith("/agent/sessions/sess_stream/messages/stream") && init?.method === "POST") {
      streamCallCount += 1;
      if (streamCallCount === 1) return comparisonProjectionStreamResponse(initialResponse, first);
      return comparisonProjectionFailureStreamResponse(failedResponse, second, "later stream failed");
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);
  fireEvent.change(screen.getAllByRole("textbox")[0], { target: { value: "initial request" } });
  fireEvent.keyDown(screen.getAllByRole("textbox")[0], { key: "Enter" });
  await waitFor(() => expect(plannerStore.getSnapshot().comparisonPreview.plans).toHaveLength(1));

  fireEvent.click(screen.getByRole("button", { name: /Continue Plan B/ }));
  await waitFor(() => expect(screen.getByText("later stream failed")).toBeTruthy());
  const preserved = plannerStore.getSnapshot().comparisonPreview;
  expect(preserved.plans.map((plan) => plan.proposalId)).toEqual(["proposal_a", "proposal_b"]);
  expect(preserved.focusedProposalId).toBe("proposal_a");
  expect(preserved.autoNavigationCount).toBe(1);
  const preservedColors = preserved.plans.map((plan) => plan.colorKey);
  expect(preservedColors[0]).toBe(stablePlanColorKey("proposal_a"));
  expect(new Set(preservedColors).size).toBe(2);
  expect(perceptualPlanColorDistance(preservedColors[0], preservedColors[1])).toBeGreaterThanOrEqual(90);
});

test("a stopped request cannot publish a late comparison projection into the current state", async () => {
  const encoder = new TextEncoder();
  let lateStreamController!: ReadableStreamDefaultController<Uint8Array>;
  const lateStreamResponse = new Response(
    new ReadableStream<Uint8Array>({
      start(controller) {
        lateStreamController = controller;
      }
    }),
    { status: 200, headers: { "Content-Type": "application/x-ndjson" } }
  );
  const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) return jsonResponse({ mode: "mock", default: [], mock: [] });
    if (path.endsWith("/preferences/extract")) return jsonResponse({ summaryCard: preferenceCard("none") });
    if (path.endsWith("/agent/sessions/current"))
      return jsonResponse({ detail: "No active conversation session" }, 404);
    if (path.endsWith("/agent/sessions")) return jsonResponse(agentSession());
    if (path.endsWith("/agent/sessions/sess_stream/messages/stream") && init?.method === "POST") {
      return lateStreamResponse;
    }
    if (path.endsWith("/agent/sessions/sess_stream/runs/cancel") && init?.method === "POST") {
      return jsonResponse({ cancelRequested: true });
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);
  fireEvent.change(screen.getByLabelText("Agent 对话文本"), {
    target: { value: "启动一个随后停止的规划" }
  });
  fireEvent.keyDown(screen.getByLabelText("Agent 对话文本"), { key: "Enter" });
  fireEvent.click(await screen.findByRole("button", { name: "停止本轮" }));
  await waitFor(() =>
    expect(within(screen.getByLabelText("Agent 规划进度")).getByText(/已停止 · 方案整理已停止/)).toBeTruthy()
  );
  const cancelledGroup = screen.getByLabelText("Agent 规划进度").closest("[data-assistant-response-group]");
  expect(cancelledGroup).toBeTruthy();
  expect(cancelledGroup?.querySelectorAll(".bot-avatar")).toHaveLength(1);
  expect(cancelledGroup?.querySelector(".reasoning-status-spinner.active")).toBeNull();
  expect(screen.queryByText(/规划失败/)).toBeNull();
  expect(screen.queryByText(/operation was aborted/i)).toBeNull();

  const lateProjection = comparisonStreamProjection("proposal_late", "迟到方案不得出现");
  const lateResponse = agentMessageResponse();
  lateResponse.assistantTurn = {
    ...lateResponse.assistantTurn,
    content: "迟到回复不得出现"
  };
  lateStreamController.enqueue(
    encoder.encode(
      `${JSON.stringify({
        event: "execution_event",
        data: {
          type: "portfolio_plan_visible",
          label: "迟到方案",
          status: "completed",
          detail: "旧请求停止后到达",
          userVisible: true,
          fallbackUsed: false,
          metadata: { comparisonProjection: lateProjection },
          timestamp: "2026-08-06T09:01:00Z"
        }
      })}\n`
    )
  );
  lateStreamController.enqueue(
    encoder.encode(`${JSON.stringify({ event: "message_response", data: lateResponse })}\n`)
  );
  lateStreamController.close();

  await new Promise((resolve) => window.setTimeout(resolve, 20));
  expect(plannerStore.getSnapshot().comparisonPreview.plans).toEqual([]);
  expect(screen.queryByText("迟到方案不得出现")).toBeNull();
  expect(screen.queryByText("迟到回复不得出现")).toBeNull();
});

test.each(["create", "switch", "delete"] as const)(
  "%s session clears the previous interrupted reasoning run and ignores its late frames",
  async (action) => {
    const previous = agentSession();
    const empty = { ...agentSession(), sessionId: "sess_empty", title: "新的空白对话", activePlanId: null };
    const encoder = new TextEncoder();
    let controller!: ReadableStreamDefaultController<Uint8Array>;
    const stream = new Response(
      new ReadableStream<Uint8Array>({
        start(value) {
          controller = value;
        }
      }),
      { headers: { "Content-Type": "application/x-ndjson" } }
    );
    const sessions = [previous, empty].map((session) => ({ ...session, turnCount: 0 }));
    plannerStore.setState({ agentSession: previous, lastPlanningRun: planningRunFixture() });
    vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      const path = String(url);
      if (path.endsWith("/providers/status")) return jsonResponse({ mode: "mock", default: [], mock: [] });
      if (path.endsWith("/preferences/extract")) return jsonResponse({ summaryCard: preferenceCard("none") });
      if (path.endsWith("/agent/sessions/current")) return jsonResponse(previous);
      if (path.endsWith("/agent/sessions")) {
        return jsonResponse(init?.method === "POST" ? empty : { sessions });
      }
      if (path.endsWith("/agent/sessions/sess_empty")) return jsonResponse(empty);
      if (path.endsWith("/agent/sessions/sess_stream") && init?.method === "DELETE") return jsonResponse({ sessions: [sessions[1]] });
      if (path.endsWith("/agent/sessions/sess_stream/messages/stream")) return stream;
      if (path.endsWith("/runs/cancel")) return jsonResponse({ cancelRequested: true });
      return jsonResponse({}, 404);
    }));

    render(<AppShell />);
    await screen.findByRole("option", { name: "新的空白对话 · 0 轮" });
    fireEvent.change(screen.getByLabelText("Agent 对话文本"), { target: { value: "旧会话中的规划" } });
    fireEvent.keyDown(screen.getByLabelText("Agent 对话文本"), { key: "Enter" });
    controller.enqueue(encoder.encode(`${JSON.stringify({
      event: "reasoning_status", data: reasoningStatus(1, "running", "正在理解需求", "context")
    })}\n`));
    await screen.findByText("正在理解需求");
    fireEvent.click(screen.getByRole("button", { name: "停止本轮" }));
    expect(screen.getByLabelText("Agent 规划进度")).toBeTruthy();

    if (action === "switch") {
      fireEvent.change(screen.getByLabelText("选择对话"), { target: { value: empty.sessionId } });
    } else {
      fireEvent.click(screen.getByRole("button", { name: action === "create" ? "新建对话" : "删除对话及行程" }));
    }
    await waitFor(() => expect(plannerStore.getSnapshot().agentSession?.sessionId ?? null).toBe(
      action === "delete" ? null : empty.sessionId
    ));
    expect(plannerStore.getSnapshot().conversationTurns).toEqual([]);
    expect(screen.queryByLabelText("Agent 规划进度")).toBeNull();
    expect(document.querySelector('[data-assistant-response-group="interrupted-agent-run"]')).toBeNull();
    expect(plannerStore.getSnapshot().lastPlanningRun).toBeNull();

    controller.enqueue(encoder.encode(`${JSON.stringify({
      event: "reasoning_status", data: reasoningStatus(2, "running", "旧会话迟到的进度", "tool")
    })}\n${JSON.stringify({ event: "user_turn", data: agentMessageResponse().userTurn })}\n${JSON.stringify({
      event: "message_response", data: agentMessageResponse()
    })}\n`));
    controller.close();
    await new Promise((resolve) => window.setTimeout(resolve, 20));
    expect(screen.queryByLabelText("Agent 规划进度")).toBeNull();
    expect(plannerStore.getSnapshot().conversationTurns).toEqual([]);
    expect(plannerStore.getSnapshot().agentSession?.sessionId ?? null).toBe(action === "delete" ? null : empty.sessionId);
  }
);

test("a stopped stream cannot start recovery after a new session is active", async () => {
  const previous = agentSession();
  const empty = { ...agentSession(), sessionId: "sess_empty", activePlanId: null };
  const lateStream = deferred<Response>();
  const response = agentMessageResponse();
  const restored = { ...previous, turns: [response.userTurn, response.assistantTurn] };
  plannerStore.setState({ agentSession: previous });
  const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) return jsonResponse({ mode: "mock", default: [], mock: [] });
    if (path.endsWith("/preferences/extract")) return jsonResponse({ summaryCard: preferenceCard("none") });
    if (path.endsWith("/agent/sessions/current")) return jsonResponse(previous);
    if (path.endsWith("/agent/sessions")) return jsonResponse(init?.method === "POST" ? empty : { sessions: [] });
    if (path.endsWith("/agent/sessions/sess_stream/messages/stream")) return lateStream.promise;
    if (path.endsWith("/runs/cancel")) return jsonResponse({ cancelRequested: true });
    if (path.includes("/reasoning-statuses")) return jsonResponse({
      sessionId: previous.sessionId, sourceUserTurnId: response.userTurn.id,
      assistantTurnId: response.assistantTurn.id, statuses: [], active: false,
      nextSequence: 1, terminalStatus: "completed"
    });
    if (path.endsWith("/agent/sessions/sess_stream")) return jsonResponse(restored);
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);
  render(<AppShell />);
  fireEvent.change(screen.getByLabelText("Agent 对话文本"), { target: { value: "等待旧会话响应" } });
  fireEvent.keyDown(screen.getByLabelText("Agent 对话文本"), { key: "Enter" });
  await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith("/messages/stream"))).toBe(true));
  fireEvent.click(screen.getByRole("button", { name: "停止本轮" }));
  fireEvent.click(screen.getByRole("button", { name: "新建对话" }));
  await waitFor(() => expect(plannerStore.getSnapshot().agentSession?.sessionId).toBe(empty.sessionId));

  lateStream.resolve(new Response("", { headers: { "Content-Type": "application/x-ndjson" } }));
  await new Promise((resolve) => window.setTimeout(resolve, 20));
  expect(fetchMock.mock.calls.filter(([url]) => String(url).includes("/reasoning-statuses"))).toHaveLength(0);
  expect(plannerStore.getSnapshot().agentSession?.sessionId).toBe(empty.sessionId);
  expect(plannerStore.getSnapshot().conversationTurns).toEqual([]);
});

test("stopping during preference extraction prevents a late request from entering the new session", async () => {
  const previous = agentSession();
  const empty = { ...agentSession(), sessionId: "sess_empty", activePlanId: null };
  const preference = deferred<Response>();
  plannerStore.setState({ agentSession: previous });
  const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) return jsonResponse({ mode: "mock", default: [], mock: [] });
    if (path.endsWith("/preferences/extract")) return preference.promise;
    if (path.endsWith("/agent/sessions/current")) return jsonResponse(previous);
    if (path.endsWith("/agent/sessions")) return jsonResponse(init?.method === "POST" ? empty : { sessions: [] });
    if (path.endsWith("/runs/cancel")) return jsonResponse({ cancelRequested: true });
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);
  render(<AppShell />);
  fireEvent.change(screen.getByLabelText("Agent 对话文本"), { target: { value: "旧请求的旅行偏好" } });
  fireEvent.keyDown(screen.getByLabelText("Agent 对话文本"), { key: "Enter" });
  await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith("/preferences/extract"))).toBe(true));
  fireEvent.click(screen.getByRole("button", { name: "停止本轮" }));
  fireEvent.click(screen.getByRole("button", { name: "新建对话" }));
  await waitFor(() => expect(plannerStore.getSnapshot().agentSession?.sessionId).toBe(empty.sessionId));
  const newPreference = plannerStore.getSnapshot().preferenceCard;
  preference.resolve(jsonResponse({ summaryCard: preferenceCard("旧请求的迟到偏好") }));
  await new Promise((resolve) => window.setTimeout(resolve, 20));
  expect(fetchMock.mock.calls.some(([url]) => String(url).includes("/messages"))).toBe(false);
  expect(plannerStore.getSnapshot().preferenceCard).toEqual(newPreference);
  expect(plannerStore.getSnapshot().conversationTurns).toEqual([]);
  expect(screen.queryByLabelText("Agent 规划进度")).toBeNull();
});

test("late initial hydration cannot restore a session after the user deleted it", async () => {
  const previous = agentSession();
  const hydration = deferred<Response>();
  plannerStore.setState({ agentSession: previous });
  const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) return jsonResponse({ mode: "mock", default: [], mock: [] });
    if (path.endsWith("/agent/sessions")) return jsonResponse({ sessions: [] });
    if (path.endsWith(`/agent/sessions/${previous.sessionId}`) && init?.method === "DELETE") return jsonResponse({ sessions: [] });
    if (path.endsWith("/agent/sessions/current") || path.endsWith(`/agent/sessions/${previous.sessionId}`)) return hydration.promise;
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);
  render(<AppShell />);
  fireEvent.click(screen.getByRole("button", { name: "删除对话及行程" }));
  await waitFor(() => expect(plannerStore.getSnapshot().agentSession).toBeNull());
  hydration.resolve(jsonResponse(previous));
  await new Promise((resolve) => window.setTimeout(resolve, 20));
  expect(plannerStore.getSnapshot().agentSession).toBeNull();
  expect(fetchMock.mock.calls.some(([url]) => String(url).includes("/reasoning-statuses"))).toBe(false);
  expect(screen.queryByLabelText("Agent 规划进度")).toBeNull();
});

test.each(["success", "failure"] as const)(
  "late initial session list %s cannot replace the list returned by deletion",
  async (outcome) => {
    const previous = agentSession();
    const survivor = { ...agentSession(), sessionId: "sess_survivor", title: "保留的对话", turnCount: 0 };
    const initialList = deferred<Response>();
    plannerStore.setState({ agentSession: previous });
    vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      const path = String(url);
      if (path.endsWith("/providers/status")) return jsonResponse({ mode: "mock", default: [], mock: [] });
      if (path.endsWith("/agent/sessions")) return initialList.promise;
      if (path.endsWith(`/agent/sessions/${previous.sessionId}`) && init?.method === "DELETE") {
        return jsonResponse({ sessions: [survivor] });
      }
      if (path.endsWith("/agent/sessions/current") || path.endsWith(`/agent/sessions/${previous.sessionId}`)) {
        return jsonResponse(previous);
      }
      return jsonResponse({}, 404);
    }));
    render(<AppShell />);
    fireEvent.click(screen.getByRole("button", { name: "删除对话及行程" }));
    await waitFor(() => expect(plannerStore.getSnapshot().agentSession).toBeNull());
    await screen.findByRole("option", { name: "保留的对话 · 0 轮" });
    const authoritativeList = plannerStore.getSnapshot().agentSessions;

    initialList.resolve(outcome === "success"
      ? jsonResponse({ sessions: [{ ...previous, turnCount: 0 }, survivor] })
      : jsonResponse({ message: "initial session list failed" }, 500));
    await new Promise((resolve) => window.setTimeout(resolve, 20));
    expect(plannerStore.getSnapshot().agentSessions).toEqual(authoritativeList);
    expect(screen.queryByRole("option", { name: "北京 AI 行程 · 0 轮" })).toBeNull();
    expect(screen.getByRole("option", { name: "保留的对话 · 0 轮" })).toBeTruthy();
  }
);

test("agent completed execution events mark the current running step completed", async () => {
  const finishStream = deferred<void>();
  const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) {
      return jsonResponse({ mode: "mock", default: [], mock: [] });
    }
    if (path.endsWith("/preferences/extract")) {
      return jsonResponse({ summaryCard: preferenceCard("用户偏好轻松不赶路。") });
    }
    if (path.endsWith("/agent/sessions/current")) {
      return jsonResponse({ detail: "No active conversation session" }, 404);
    }
    if (path.endsWith("/agent/sessions")) {
      return jsonResponse(agentSession());
    }
    if (path.endsWith("/agent/sessions/sess_stream/messages/stream") && init?.method === "POST") {
      return agentCompletedExecutionStreamResponse(finishStream.promise);
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);
  fireEvent.change(screen.getByLabelText("Agent 对话文本"), {
    target: { value: "北京两天，轻松一点，想去故宫和胡同" }
  });
  fireEvent.keyDown(screen.getByLabelText("Agent 对话文本"), { key: "Enter" });

  await waitFor(() => expect(screen.getByText(/已完成上下文分析/)).toBeTruthy());
  const progress = screen.getByLabelText("Agent 规划进度");
  expect(progress.getAttribute("data-message-type")).toBe("reasoning_status");
  expect(within(progress).queryByText("read_itinerary")).toBeNull();
  expect(within(progress).queryByText("执行 Agent 工具循环")).toBeNull();

  finishStream.resolve();
  await waitFor(() => expect(screen.getAllByText("已按规划过程生成可编辑行程。").length).toBeGreaterThan(0));
  await waitFor(() => expect(screen.getByLabelText("Agent turn planning process")).toBeTruthy());
  expect(screen.getByText("查看详细执行记录")).toBeTruthy();
  expect(screen.getByText("read_itinerary")).toBeTruthy();
});

test("real reasoning status and final answer share one assistant response group with one terminal fact", async () => {
  const finishStream = deferred<void>();
  const response = agentMessageResponse();
  response.assistantTurn.reasoningStatuses = [
    reasoningStatus(1, "running", "正在核验真实地图地点", "tool"),
    reasoningStatus(2, "completed", "处理完成，正在展示结果", "finalizing")
  ];
  response.reasoningStatuses = response.assistantTurn.reasoningStatuses;
  const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) {
      return jsonResponse({ mode: "mock", default: [], mock: [] });
    }
    if (path.endsWith("/preferences/extract")) {
      return jsonResponse({ summaryCard: preferenceCard("用户偏好轻松不赶路。") });
    }
    if (path.endsWith("/agent/sessions/current")) {
      return jsonResponse({ detail: "No active conversation session" }, 404);
    }
    if (path.endsWith("/agent/sessions")) {
      return jsonResponse(agentSession());
    }
    if (path.endsWith("/agent/sessions/sess_stream/messages/stream") && init?.method === "POST") {
      return agentReasoningStreamResponse(response, finishStream.promise);
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);
  fireEvent.change(screen.getByLabelText("Agent 对话文本"), {
    target: { value: "北京两天，核验真实地点后再给答案" }
  });
  fireEvent.keyDown(screen.getByLabelText("Agent 对话文本"), { key: "Enter" });

  const progress = await screen.findByLabelText("Agent 规划进度");
  expect(progress.getAttribute("data-message-type")).toBe("reasoning_status");
  expect(within(progress).getByText("正在核验真实地图地点")).toBeTruthy();
  expect(screen.queryByText("已按规划过程生成可编辑行程。")).toBeNull();
  const liveResponseGroup = document.querySelector(".chat-row.assistant.streaming") as HTMLElement;
  expect(liveResponseGroup).toBeTruthy();
  expect(liveResponseGroup.querySelectorAll(".bot-avatar")).toHaveLength(1);
  expect(within(liveResponseGroup).getByLabelText("Agent 规划进度")).toBeTruthy();

  finishStream.resolve();
  await waitFor(() => expect(screen.getAllByText("已按规划过程生成可编辑行程。").length).toBeGreaterThan(0));
  const reasoningPanels = screen.getAllByLabelText("Agent 规划进度");
  const history = reasoningPanels[reasoningPanels.length - 1] as HTMLElement;
  expect(within(history).getByText(/规划用时 \d+ 秒 · 处理完成，正在展示结果/)).toBeTruthy();
  expect(within(history).queryByRole("button")).toBeNull();
  const responseGroup = history.closest("[data-assistant-response-group]") as HTMLElement;
  expect(responseGroup).toBeTruthy();
  expect(responseGroup.querySelectorAll(".bot-avatar")).toHaveLength(1);
  expect(within(responseGroup).getByText("已按规划过程生成可编辑行程。")).toBeTruthy();
  expect(within(responseGroup).queryByText(/个阶段/)).toBeNull();
  expect(within(history).queryByText("hidden chain of thought")).toBeNull();
});

test("safe no-op completes in one assistant response group instead of appearing as a failure", async () => {
  const response = agentMessageResponse();
  response.terminalStatus = "no_safe_action";
  response.assistantTurn = {
    ...response.assistantTurn,
    content: "当前消息没有给出具体日期或地点；为避免误改，我保留了现有行程。",
    status: "active",
    reasoningStatuses: [
      reasoningStatus(1, "completed", "已确认本轮信息不足，不修改现有行程", "constraints"),
      reasoningStatus(2, "completed", "处理完成，正在展示结果", "finalizing")
    ]
  };
  response.reasoningStatuses = response.assistantTurn.reasoningStatuses;
  const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) return jsonResponse({ mode: "mock", default: [], mock: [] });
    if (path.endsWith("/preferences/extract")) return jsonResponse({ summaryCard: preferenceCard("none") });
    if (path.endsWith("/agent/sessions/current"))
      return jsonResponse({ detail: "No active conversation session" }, 404);
    if (path.endsWith("/agent/sessions")) return jsonResponse(agentSession());
    if (path.endsWith("/agent/sessions/sess_stream/messages/stream") && init?.method === "POST") {
      return agentReasoningStreamResponse(response, Promise.resolve());
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);
  fireEvent.change(screen.getByLabelText("Agent 对话文本"), { target: { value: "看看现在的行程" } });
  fireEvent.keyDown(screen.getByLabelText("Agent 对话文本"), { key: "Enter" });

  const answer = await screen.findByText("当前消息没有给出具体日期或地点；为避免误改，我保留了现有行程。");
  const group = answer.closest("[data-assistant-response-group]") as HTMLElement;
  expect(group).toBeTruthy();
  expect(group.querySelectorAll(".bot-avatar")).toHaveLength(1);
  expect(within(group).getByText(/规划用时 \d+ 秒 · 处理完成，正在展示结果/)).toBeTruthy();
  expect(within(group).queryByText(/规划失败|处理未完成/)).toBeNull();
});

test("an interrupted NDJSON connection resumes by cursor without resending the user request", async () => {
  const response = agentMessageResponse();
  response.assistantTurn.reasoningStatuses = [
    reasoningStatus(1, "completed", "已完成约束与执行策略检查", "constraints"),
    reasoningStatus(2, "completed", "处理完成，正在展示结果", "finalizing")
  ];
  const restoredSession: AgentSession = {
    ...agentSession(),
    turns: [response.userTurn, response.assistantTurn]
  };
  let reasoningSnapshotCalls = 0;
  const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) {
      return jsonResponse({ mode: "mock", default: [], mock: [] });
    }
    if (path.endsWith("/preferences/extract")) {
      return jsonResponse({ summaryCard: preferenceCard("用户偏好轻松不赶路。") });
    }
    if (path.endsWith("/agent/sessions/current")) {
      return jsonResponse({ detail: "No active conversation session" }, 404);
    }
    if (path.includes("/agent/sessions/sess_stream/reasoning-statuses")) {
      reasoningSnapshotCalls += 1;
      return jsonResponse({
        sessionId: "sess_stream",
        sourceUserTurnId: response.userTurn.id,
        assistantTurnId: response.assistantTurn.id,
        statuses: response.assistantTurn.reasoningStatuses,
        active: false,
        nextSequence: 2,
        terminalStatus: "completed"
      });
    }
    if (path.endsWith("/agent/sessions/sess_stream") && (!init?.method || init.method === "GET")) {
      return jsonResponse(restoredSession);
    }
    if (path.endsWith("/agent/sessions")) {
      return jsonResponse(agentSession());
    }
    if (path.endsWith("/agent/sessions/sess_stream/messages/stream") && init?.method === "POST") {
      return truncatedReasoningStreamResponse(response);
    }
    if (path.endsWith("/agent/sessions/sess_stream/messages") && init?.method === "POST") {
      throw new Error("the recovery path must never replay the user request");
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);
  fireEvent.change(screen.getByLabelText("Agent 对话文本"), {
    target: { value: "北京两天，连接中断后恢复当前处理" }
  });
  fireEvent.keyDown(screen.getByLabelText("Agent 对话文本"), { key: "Enter" });

  await waitFor(() => expect(screen.getAllByText("已按规划过程生成可编辑行程。").length).toBeGreaterThan(0));
  expect(reasoningSnapshotCalls).toBe(1);
  expect(
    fetchMock.mock.calls.filter(
      ([url, init]) =>
        String(url).endsWith("/agent/sessions/sess_stream/messages/stream") && (init as RequestInit | undefined)?.method === "POST"
    )
  ).toHaveLength(1);
  expect(
    fetchMock.mock.calls.some(
      ([url, init]) =>
        String(url).endsWith("/agent/sessions/sess_stream/messages") && (init as RequestInit | undefined)?.method === "POST"
    )
  ).toBe(false);
  expect(screen.getByLabelText("Agent 规划进度")).toBeTruthy();
});

test("recovery before user_turn binds its cursor to the current server run instead of prior history", async () => {
  const response = agentMessageResponse();
  response.assistantTurn.reasoningStatuses = [
    reasoningStatus(1, "completed", "已完成约束检查", "constraints"),
    reasoningStatus(2, "completed", "处理完成，正在展示结果", "finalizing")
  ];
  const reasoningStatuses = response.assistantTurn.reasoningStatuses;
  if (!reasoningStatuses || reasoningStatuses.length < 2) {
    throw new Error("reasoning recovery fixture requires two statuses");
  }
  const restoredSession: AgentSession = {
    ...agentSession(),
    turns: [response.userTurn, response.assistantTurn]
  };
  let reasoningSnapshotCalls = 0;
  const statusUrls: string[] = [];
  const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) {
      return jsonResponse({ mode: "mock", default: [], mock: [] });
    }
    if (path.endsWith("/preferences/extract")) {
      return jsonResponse({ summaryCard: preferenceCard("用户偏好轻松不赶路。") });
    }
    if (path.endsWith("/agent/sessions/current")) {
      return jsonResponse({ detail: "No active conversation session" }, 404);
    }
    if (path.includes("/agent/sessions/sess_stream/reasoning-statuses")) {
      reasoningSnapshotCalls += 1;
      statusUrls.push(path);
      if (reasoningSnapshotCalls === 1) {
        return jsonResponse({
          sessionId: "sess_stream",
          sourceUserTurnId: null,
          assistantTurnId: null,
          statuses: [],
          active: true,
          nextSequence: 0,
          terminalStatus: null
        });
      }
      if (reasoningSnapshotCalls === 2) {
        return jsonResponse({
          sessionId: "sess_stream",
          sourceUserTurnId: response.userTurn.id,
          assistantTurnId: null,
          statuses: [reasoningStatuses[0]],
          active: true,
          nextSequence: 1,
          terminalStatus: null
        });
      }
      return jsonResponse({
        sessionId: "sess_stream",
        sourceUserTurnId: response.userTurn.id,
        assistantTurnId: response.assistantTurn.id,
        statuses: [reasoningStatuses[1]],
        active: false,
        nextSequence: 2,
        terminalStatus: "completed"
      });
    }
    if (path.endsWith("/agent/sessions/sess_stream") && (!init?.method || init.method === "GET")) {
      return jsonResponse(restoredSession);
    }
    if (path.endsWith("/agent/sessions")) {
      return jsonResponse(agentSession());
    }
    if (path.endsWith("/agent/sessions/sess_stream/messages/stream") && init?.method === "POST") {
      return preUserTurnInterruptedStreamResponse();
    }
    if (path.endsWith("/agent/sessions/sess_stream/messages") && init?.method === "POST") {
      throw new Error("the recovery path must never replay the user request");
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);
  fireEvent.change(screen.getByLabelText("Agent 对话文本"), {
    target: { value: "北京两天，断线发生在 user_turn 帧之前" }
  });
  fireEvent.keyDown(screen.getByLabelText("Agent 对话文本"), { key: "Enter" });

  await waitFor(
    () => expect(screen.getAllByText("已按规划过程生成可编辑行程。").length).toBeGreaterThan(0),
    { timeout: 4000 }
  );
  expect(reasoningSnapshotCalls).toBe(3);
  expect(statusUrls[0]).not.toContain("turnId=");
  expect(statusUrls[1]).not.toContain("turnId=");
  expect(statusUrls[2]).toContain(`turnId=${response.userTurn.id}`);
  expect(statusUrls[2]).toContain("afterSequence=1");
  expect(
    fetchMock.mock.calls.filter(
      ([url, init]) =>
        String(url).endsWith("/agent/sessions/sess_stream/messages/stream") &&
        (init as RequestInit | undefined)?.method === "POST"
    )
  ).toHaveLength(1);
});

test("agent execution failed events surface the current failure state", async () => {
  const finishStream = deferred<void>();
  const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) {
      return jsonResponse({ mode: "mock", default: [], mock: [] });
    }
    if (path.endsWith("/preferences/extract")) {
      return jsonResponse({ summaryCard: preferenceCard("用户偏好轻松不赶路。") });
    }
    if (path.endsWith("/agent/sessions/current")) {
      return jsonResponse({ detail: "No active conversation session" }, 404);
    }
    if (path.endsWith("/agent/sessions")) {
      return jsonResponse(agentSession());
    }
    if (path.endsWith("/agent/sessions/sess_stream/messages/stream") && init?.method === "POST") {
      return agentExecutionFailureStreamResponse(finishStream.promise);
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);
  fireEvent.change(screen.getByLabelText("Agent 对话文本"), {
    target: { value: "把行程调整得轻松一点" }
  });
  fireEvent.keyDown(screen.getByLabelText("Agent 对话文本"), { key: "Enter" });

  await waitFor(() => expect(screen.getAllByText(/处理用时 \d+ 秒/).length).toBeGreaterThan(0));
  const progress = screen.getByLabelText("Agent 规划进度");
  expect(progress.getAttribute("data-message-type")).toBe("reasoning_status");
  expect(screen.queryByText("发送中")).toBeNull();

  finishStream.resolve();
  await waitFor(() => expect(screen.getAllByText("已按规划过程生成可编辑行程。").length).toBeGreaterThan(0));
  await waitFor(() => expect(screen.getByLabelText("Agent turn planning process")).toBeTruthy());
  expect(screen.getByText("查看详细执行记录")).toBeTruthy();
  expect(screen.getByText("联网搜索票务/预约")).toBeTruthy();
});

test("agent stream error events do not fall back to a duplicate message request", async () => {
  const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) {
      return jsonResponse({ mode: "mock", default: [], mock: [] });
    }
    if (path.endsWith("/preferences/extract")) {
      return jsonResponse({ summaryCard: preferenceCard("用户偏好轻松不赶路。") });
    }
    if (path.endsWith("/agent/sessions/current")) {
      return jsonResponse({ detail: "No active conversation session" }, 404);
    }
    if (path.endsWith("/agent/sessions")) {
      return jsonResponse(agentSession());
    }
    if (path.endsWith("/agent/sessions/sess_stream/messages/stream") && init?.method === "POST") {
      return agentExecutionErrorStreamResponse("stream business 404", 404);
    }
    if (path.endsWith("/agent/sessions/sess_stream/messages") && init?.method === "POST") {
      return jsonResponse(agentMessageResponse());
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);
  fireEvent.change(screen.getByLabelText("Agent 对话文本"), {
    target: { value: "北京两天，轻松一点，想去故宫和胡同" }
  });
  fireEvent.keyDown(screen.getByLabelText("Agent 对话文本"), { key: "Enter" });

  await waitFor(() => expect(screen.getByText("stream business 404")).toBeTruthy());
  expect(
    fetchMock.mock.calls.some((call) => String(call[0]).endsWith("/agent/sessions/sess_stream/messages/stream"))
  ).toBe(true);
  expect(fetchMock.mock.calls.some((call) => String(call[0]).endsWith("/agent/sessions/sess_stream/messages"))).toBe(
    false
  );
});
test("Enter sends a non-empty agent message and shows completed planning steps after success", async () => {
  plannerStore.setState({
    selectedCity: "北京",
    agentSession: null,
    conversationTurns: [],
    itineraryPlan: null,
    planComparison: null,
    preferenceCard: null,
    pendingPoiCandidates: [],
    activeVersionId: null,
    selectedSegmentId: null,
    selectedRouteOptionId: null,
    previewRouteOptionId: null
  });
  const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) {
      return jsonResponse({ mode: "mock", default: [], mock: [] });
    }
    if (path.endsWith("/preferences/extract")) {
      return jsonResponse({ summaryCard: preferenceCard("用户偏好轻松不赶路，预算约 3000 元，公共交通优先。") });
    }
    if (path.endsWith("/agent/sessions/current")) {
      return jsonResponse({ detail: "No active conversation session" }, 404);
    }
    if (path.endsWith("/agent/sessions")) {
      return jsonResponse(agentSession());
    }
    if (path.endsWith("/agent/sessions/sess_stream/messages")) {
      return jsonResponse(agentMessageResponse());
    }
    if (path.endsWith("/itineraries/plan_stream/patch")) {
      return jsonResponse(localReplanPatchResponse());
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);

  fireEvent.change(screen.getByLabelText("Agent 对话文本"), {
    target: { value: "北京两天，轻松一点，想去故宫和胡同" }
  });
  fireEvent.keyDown(screen.getByLabelText("Agent 对话文本"), { key: "Enter" });
  fireEvent.keyDown(screen.getByLabelText("Agent 对话文本"), { key: "Enter" });

  await waitFor(() => expect(screen.getAllByText("已按规划过程生成可编辑行程。").length).toBeGreaterThan(0));
  await waitFor(() => expect(screen.getAllByRole("heading", { name: "行程摘要" }).length).toBeGreaterThan(0));
  expect(screen.getAllByRole("table").length).toBeGreaterThan(0);
  expect(screen.getAllByRole("columnheader", { name: "时段" }).length).toBeGreaterThan(0);
  expect(screen.getAllByRole("columnheader", { name: "安排" }).length).toBeGreaterThan(0);
  expect(screen.getAllByRole("cell", { name: "故宫" }).length).toBeGreaterThan(0);
  expect(screen.queryByText(/tool_calls|resultPreview|toolEvents/)).toBeNull();
  expect(screen.queryByLabelText("Agent planning process")).toBeNull();
  expect(screen.getAllByRole("link", { name: "官方入口" }).length).toBeGreaterThan(0);
  const turnPanel = screen.getByLabelText("Agent turn planning process");
  expect(within(turnPanel).getByText("查看详细执行记录")).toBeTruthy();
  expect(within(turnPanel).getByText("高德天气")).toBeTruthy();
  expect(within(turnPanel).getByText("联网搜索票务/预约")).toBeTruthy();
  expect(within(turnPanel).getByText("read_itinerary")).toBeTruthy();
  expect(within(turnPanel).getByText("patch_itinerary")).toBeTruthy();
  expect(within(turnPanel).getAllByText("失败").length).toBeGreaterThan(0);
  fireEvent.click(within(turnPanel).getByText("查看失败详情"));
  expect(within(turnPanel).getAllByText("工具输入").length).toBeGreaterThan(0);
  expect(within(turnPanel).getAllByText("返回结果").length).toBeGreaterThan(0);
  expect(within(turnPanel).getByText(/budgetTarget: Input should be a valid number/)).toBeTruthy();
  expect(within(turnPanel).getAllByText(/相关服务暂时不可用，当前结果可能不完整/).length).toBeGreaterThan(0);
  expect(screen.queryByText(/provider/)).toBeNull();
  expect(screen.queryByText(/fallback/)).toBeNull();
  expect(screen.queryByText(/WEB_SEARCH_API_KEY/)).toBeNull();
  expect(screen.queryByText("服务端时间轴草稿写入")).toBeNull();

  const messageCalls = (fetchMock.mock.calls as Array<[RequestInfo | URL, RequestInit?]>).filter((call) =>
    String(call[0]).endsWith("/agent/sessions/sess_stream/messages")
  );
  expect(messageCalls).toHaveLength(1);
  const messageBody = JSON.parse(String(messageCalls[0][1]?.body));
  expect(messageBody.content).toBe("北京两天，轻松一点，想去故宫和胡同");
  expect(messageBody.context.currentUserMessage).toBe("北京两天，轻松一点，想去故宫和胡同");
  expect(messageBody.context.currentPreferenceSummary).toContain("预算约 3000 元");
});

test("late current-session hydration does not overwrite a newer agent response", async () => {
  plannerStore.setState({
    selectedCity: "北京",
    agentSession: null,
    conversationTurns: [],
    itineraryPlan: null,
    planComparison: null,
    preferenceCard: null,
    pendingPoiCandidates: [],
    activeVersionId: null,
    selectedSegmentId: null,
    selectedRouteOptionId: null,
    previewRouteOptionId: null
  });
  const currentSession = deferred<Response>();
  const fetchMock = vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) {
      return jsonResponse({ mode: "mock", default: [], mock: [] });
    }
    if (path.endsWith("/preferences/memory")) {
      return jsonResponse({
        userId: "test-user",
        memoryText: "",
        autoUpdateEnabled: true,
        createdAt: "2026-06-10T10:00:00Z",
        updatedAt: "2026-06-10T10:00:00Z"
      });
    }
    if (path.endsWith("/preferences/extract")) {
      return jsonResponse({ summaryCard: preferenceCard("轻松不赶路。") });
    }
    if (path.endsWith("/agent/sessions/current")) {
      return currentSession.promise;
    }
    if (path.endsWith("/agent/sessions")) {
      return jsonResponse(agentSession());
    }
    if (path.endsWith("/agent/sessions/sess_stream/messages")) {
      return jsonResponse(agentMessageResponse());
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);

  fireEvent.change(screen.getByLabelText("Agent 对话文本"), {
    target: { value: "北京一天，只去故宫" }
  });
  fireEvent.keyDown(screen.getByLabelText("Agent 对话文本"), { key: "Enter" });

  await waitFor(() => expect(plannerStore.getSnapshot().activeVersionId).toBe("ver_stream"));
  expect(plannerStore.getSnapshot().itineraryPlan?.title).toBe("北京地图行程草案");

  currentSession.resolve(jsonResponse(staleAgentSession()));

  await waitFor(() => expect(plannerStore.getSnapshot().activeVersionId).toBe("ver_stream"));
  expect(plannerStore.getSnapshot().agentSession?.sessionId).toBe("sess_stream");
  expect(plannerStore.getSnapshot().itineraryPlan?.title).toBe("北京地图行程草案");
});

test("auto-created agent session sends context from the new empty session", async () => {
  const oldPlan = {
    id: "plan_old_shanghai",
    title: "上海旧行程",
    city: "上海",
    templateType: "custom",
    budgetTarget: 3000,
    budgetEstimate: 0,
    budgetDeltaExplanation: "",
    decisionRationale: "",
    status: "draft",
    days: [],
    routeOptions: [],
    weatherSignals: [],
    trafficCrowdingSignals: [],
    ticketLookupResults: []
  };
  plannerStore.setState({
    selectedCity: "北京",
    agentSession: {
      sessionId: "sess_old_shanghai",
      status: "active",
      city: "上海",
      title: "上海旧会话",
      activePlanId: "plan_old_shanghai",
      activeVersionId: "ver_old_shanghai",
      turns: [],
      itinerary: oldPlan,
      pendingPoiCandidates: []
    },
    agentSessions: [],
    conversationTurns: [],
    itineraryPlan: oldPlan,
    itineraryAgentContext: {
      tripTitle: "上海旧行程",
      days: [],
      tripTotals: { durationMinutes: 0, estimatedCost: 0, walkingDistanceMeters: 0 }
    },
    planComparison: null,
    preferenceCard: preferenceCard("上海旧偏好：喜欢夜景和高强度路线。"),
    preferenceMemory: {
      userId: "test-user",
      sessionId: "sess_old_shanghai",
      memoryText: "# 我的旅行偏好\n\n## 旅行节奏\n- 上海旧偏好：喜欢夜景和高强度路线。\n",
      autoUpdateEnabled: true,
      createdAt: "2026-06-10T10:00:00Z",
      updatedAt: "2026-06-10T10:00:00Z"
    },
    pendingPoiCandidates: [],
    activeVersionId: "ver_old_shanghai",
    selectedSegmentId: null,
    selectedRouteOptionId: null,
    previewRouteOptionId: null
  });
  const newSession = {
    sessionId: "sess_new_beijing",
    status: "active",
    city: "北京",
    title: "北京 AI 行程",
    activePlanId: "plan_new_beijing",
    activeVersionId: null,
    turns: [],
    itinerary: null,
    pendingPoiCandidates: []
  };
  const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) {
      return jsonResponse({ mode: "mock", default: [], mock: [] });
    }
    if (path.endsWith("/preferences/memory")) {
      return jsonResponse({
        userId: "test-user",
        memoryText: "",
        autoUpdateEnabled: true,
        createdAt: "2026-06-10T10:00:00Z",
        updatedAt: "2026-06-10T10:00:00Z"
      });
    }
    if (path.endsWith("/preferences/extract")) {
      return jsonResponse({ summaryCard: { ...preferenceCard(""), items: [], summaryText: "" } });
    }
    if (path.endsWith("/agent/sessions/current")) {
      return jsonResponse({ detail: "No active conversation session" }, 404);
    }
    if (path.endsWith("/agent/sessions") && !init?.method) {
      return jsonResponse({ sessions: [] });
    }
    if (path.endsWith("/agent/sessions") && init?.method === "POST") {
      return jsonResponse(newSession);
    }
    if (path.endsWith("/agent/sessions/sess_new_beijing/messages")) {
      return jsonResponse(agentMessageResponse());
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);

  fireEvent.change(screen.getByLabelText("Agent 对话文本"), {
    target: { value: "北京一天，只去故宫" }
  });
  fireEvent.keyDown(screen.getByLabelText("Agent 对话文本"), { key: "Enter" });

  await waitFor(() =>
    expect(
      fetchMock.mock.calls.some((call) => String(call[0]).endsWith("/agent/sessions/sess_new_beijing/messages"))
    ).toBe(true)
  );
  const messageCall = fetchMock.mock.calls.find((call) =>
    String(call[0]).endsWith("/agent/sessions/sess_new_beijing/messages")
  );
  const messageBody = JSON.parse(String(messageCall?.[1]?.body));
  expect(messageBody.context.activeVersionId).toBeNull();
  expect(messageBody.context.itineraryPlan).toBeNull();
  expect(messageBody.context.timelineContext.activeVersionId).toBeNull();
  expect(messageBody.context.timelineContext.itineraryPlan).toBeNull();
  expect(JSON.stringify(messageBody.context)).not.toContain("上海旧行程");
  expect(JSON.stringify(messageBody.context)).not.toContain("ver_old_shanghai");
  expect(JSON.stringify(messageBody.context)).not.toContain("上海旧偏好");
  expect(messageBody.context.memoryText).toBe("");
  expect(messageBody.context.preferenceMemory).toBeNull();
});

test("visible planning constraints do not invent budget transport or pending defaults", async () => {
  plannerStore.setState({
    selectedCity: "北京",
    agentSession: null,
    conversationTurns: [],
    itineraryPlan: null,
    planComparison: null,
    preferenceCard: null,
    preferenceMemory: null,
    pendingPoiCandidates: [],
    activeVersionId: null,
    selectedSegmentId: null,
    selectedRouteOptionId: null,
    previewRouteOptionId: null,
    itineraryAgentContext: null,
    timelineCopyText: ""
  });
  const fetchMock = vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) {
      return jsonResponse({ mode: "mock", default: [], mock: [] });
    }
    if (path.endsWith("/preferences/memory")) {
      return jsonResponse({
        userId: "test-user",
        memoryText: "",
        autoUpdateEnabled: true,
        createdAt: "2026-06-10T10:00:00Z",
        updatedAt: "2026-06-10T10:00:00Z"
      });
    }
    if (path.endsWith("/preferences/extract")) {
      return jsonResponse({
        summaryCard: { ...preferenceCard(""), budgetRange: "", pacePreference: "", items: [], summaryText: "" }
      });
    }
    if (path.endsWith("/agent/sessions/current")) {
      return jsonResponse({ detail: "No active conversation session" }, 404);
    }
    if (path.endsWith("/agent/sessions")) {
      return jsonResponse(agentSession());
    }
    if (path.endsWith("/agent/sessions/sess_stream/messages")) {
      return jsonResponse(agentMessageResponse());
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);

  fireEvent.change(screen.getByLabelText("Agent 对话文本"), {
    target: { value: "北京一天，只去故宫" }
  });
  fireEvent.keyDown(screen.getByLabelText("Agent 对话文本"), { key: "Enter" });

  await waitFor(() => expect(screen.queryByLabelText("Agent planning process")).toBeNull());
  const messageCalls = (fetchMock.mock.calls as Array<[RequestInfo | URL, RequestInit?]>).filter((call) =>
    String(call[0]).endsWith("/agent/sessions/sess_stream/messages")
  );
  expect(messageCalls.length).toBe(1);
  const requestInit = messageCalls[0]?.[1] as RequestInit | undefined;
  const requestBody = JSON.parse(String(requestInit?.body));
  expect(requestBody.context.currentPreferenceSummary).toBe("");
  expect(requestBody.context.memoryText).toBe("");
  expect(requestBody.context.preferenceCard).toBeNull();
  expect(JSON.stringify(requestBody.context)).not.toContain("暂无明确记录");
  expect(JSON.stringify(requestBody.context)).not.toContain("通用自由行");
});

test("Shift Enter keeps a newline and blank Enter does not submit", async () => {
  const onSubmit = vi.fn();
  render(<InspirationInput onSubmit={onSubmit} />);

  const textarea = screen.getByLabelText("Agent 对话文本");
  fireEvent.keyDown(textarea, { key: "Enter" });
  expect(onSubmit).not.toHaveBeenCalled();

  fireEvent.change(textarea, { target: { value: "第一行" } });
  fireEvent.keyDown(textarea, { key: "Enter", shiftKey: true });
  expect(onSubmit).not.toHaveBeenCalled();

  fireEvent.change(textarea, { target: { value: "第一行\n第二行" } });
  fireEvent.keyDown(textarea, { key: "Enter" });
  await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
  expect(onSubmit.mock.calls[0][0].textItems[0]).toBe("第一行\n第二行");
});

test("InspirationInput strips copied chat chrome only from a leading chrome block", async () => {
  const onSubmit = vi.fn().mockResolvedValue(true);
  render(<InspirationInput onSubmit={onSubmit} />);

  const textarea = screen.getByLabelText("Agent 对话文本");
  fireEvent.change(textarea, {
    target: {
      value:
        "对话\n复制测试信息\n‹\n今年国庆参观北京高校两日游，晚上看一次北京夜景。\n正文里保留“复制测试信息”这几个字。"
    }
  });
  fireEvent.keyDown(textarea, { key: "Enter" });

  await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
  expect(onSubmit.mock.calls[0][0].textItems).toEqual([
    "今年国庆参观北京高校两日游，晚上看一次北京夜景。\n正文里保留“复制测试信息”这几个字。"
  ]);
});

test("assistant clarification options are clickable and continue the same agent session", async () => {
  plannerStore.setState({
    selectedCity: "北京",
    agentSession: null,
    conversationTurns: [],
    itineraryPlan: null,
    planComparison: null,
    preferenceCard: null,
    preferenceMemory: null,
    pendingPoiCandidates: [],
    activeVersionId: null,
    selectedSegmentId: null,
    selectedRouteOptionId: null,
    previewRouteOptionId: null
  });
  const clarificationSession = agentSessionWithClarificationOptions();
  const clarificationAssistant = clarificationSession.turns[1] as unknown as {
    content: string;
    choiceOptions: AgentChoiceOption[];
  };
  clarificationAssistant.content = "夜景偏好还不明确：更看重哪一种体验？";
  clarificationAssistant.choiceOptions = [
    {
      id: "clarify:night-experience:public-waterfront",
      index: 1,
      kind: "clarification_checkpoint",
      action: "continue_clarification",
      label: "开放滨水夜游",
      value: { semanticValue: "public_waterfront" },
      semanticValue: "public_waterfront",
      dimensionId: "night_experience",
      checkpointId: "checkpoint-night-01"
    },
    {
      id: "clarify:night-experience:city-view",
      index: 2,
      kind: "clarification_checkpoint",
      action: "continue_clarification",
      label: "城市观景平台",
      value: { semanticValue: "public_city_view" },
      semanticValue: "public_city_view",
      dimensionId: "night_experience",
      checkpointId: "checkpoint-night-01"
    }
  ];
  const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) {
      return jsonResponse({ mode: "mock", default: [], mock: [] });
    }
    if (path.endsWith("/preferences/memory")) {
      return jsonResponse({
        userId: "test-user",
        memoryText: "# 我的旅行偏好\n\n## 旅行节奏\n- 暂无明确记录。\n",
        autoUpdateEnabled: true,
        createdAt: "2026-06-10T10:00:00Z",
        updatedAt: "2026-06-10T10:00:00Z"
      });
    }
    if (path.endsWith("/preferences/extract")) {
      return jsonResponse({ summaryCard: { ...preferenceCard(""), items: [], summaryText: "" } });
    }
    if (path.endsWith("/agent/sessions/current")) {
      return jsonResponse(clarificationSession);
    }
    if (path.endsWith("/agent/sessions/sess_stream/messages") && init?.method === "POST") {
      return jsonResponse(agentMessageResponse());
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);

  await waitFor(() => expect(screen.getByRole("button", { name: /城市观景平台/ })).toBeTruthy());
  fireEvent.click(document.querySelector('[data-choice-id="clarify:night-experience:city-view"]') as HTMLButtonElement);

  await waitFor(() =>
    expect(fetchMock.mock.calls.some((call) => String(call[0]).endsWith("/agent/sessions/sess_stream/messages"))).toBe(
      true
    )
  );
  const messageCall = fetchMock.mock.calls.find((call) =>
    String(call[0]).endsWith("/agent/sessions/sess_stream/messages")
  );
  const messageBody = JSON.parse(String(messageCall?.[1]?.body));
  expect(messageBody.content).toBe("城市观景平台");
  expect(messageBody.context.selectedAgentChoice).toEqual({
    sourceAssistantTurnId: "turn_option_assistant",
    choiceId: "clarify:night-experience:city-view"
  });
  expect(messageBody.context.currentPreferenceSummary).toBe("");
  expect(messageBody.context.preferenceCard).toBeNull();
  expect(JSON.stringify(messageBody.context)).not.toContain("暂无明确记录");
});

test("v2 clarification card sends one atomic batch request with the persisted submit capability", async () => {
  const session = agentSessionWithClarificationOptions();
  const assistant = session.turns[1] as unknown as {
    content: string;
    choiceOptions: AgentChoiceOption[];
    clarificationCheckpoint: Record<string, unknown>;
  };
  assistant.content = "请一次确认本轮关键路线决策。";
  assistant.clarificationCheckpoint = {
    schemaVersion: "clarification-checkpoint-v2",
    checkpointId: "checkpoint-route-batch",
    planningRootId: "turn_option_user",
    sourceAssistantTurnId: "turn_option_assistant",
    fingerprint: "fingerprint-route-batch",
    submissionMode: "batch_atomic",
    submitChoiceId: "clarification-batch:checkpoint-route-batch",
    status: "awaiting_answer",
    questions: [
      {
        dimensionId: "route_decision.mobility_profile",
        question: "希望采用哪种主要交通方式？",
        whyItMatters: "会改变真实路线比较。",
        required: true,
        allowFreeText: false,
        options: [
          { id: "transit", label: "公共交通为主", semanticValue: { mobilityProfile: { transportMode: "transit" } } },
          { id: "walking", label: "步行为主", semanticValue: { mobilityProfile: { transportMode: "walking" } } }
        ]
      },
      {
        dimensionId: "route_decision.detour_tolerance",
        question: "更看重少绕路还是体验变化？",
        whyItMatters: "会限制路线可接受绕行。",
        required: true,
        allowFreeText: false,
        options: [
          { id: "less_detour", label: "尽量少绕路", semanticValue: { detourTolerance: { maxDetourRatio: 0.15 } } },
          { id: "more_variety", label: "接受少量绕路", semanticValue: { detourTolerance: { maxDetourRatio: 0.3 } } }
        ]
      }
    ]
  };
  assistant.choiceOptions = [
    {
      id: "clarification-batch:checkpoint-route-batch",
      action: "submit_clarification_batch",
      kind: "clarification_batch_submit",
      scopeKind: "clarification",
      label: "确认并开始规划",
      checkpointId: "checkpoint-route-batch",
      checkpointFingerprint: "fingerprint-route-batch",
      planningSelectionRootTurnId: "turn_option_user"
    }
  ];
  const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) return jsonResponse({ mode: "mock", default: [], mock: [] });
    if (path.endsWith("/preferences/memory")) {
      return jsonResponse({
        userId: "test-user",
        memoryText: "# 我的旅行偏好\n",
        autoUpdateEnabled: true,
        createdAt: "2026-06-10T10:00:00Z",
        updatedAt: "2026-06-10T10:00:00Z"
      });
    }
    if (path.endsWith("/preferences/extract")) {
      return jsonResponse({ summaryCard: { ...preferenceCard(""), items: [], summaryText: "" } });
    }
    if (path.endsWith("/agent/sessions/current")) return jsonResponse(session);
    if (path.endsWith("/agent/sessions/sess_stream/messages") && init?.method === "POST") {
      return jsonResponse(agentMessageResponse());
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);
  fireEvent.click(await screen.findByRole("radio", { name: "公共交通为主" }));
  fireEvent.click(screen.getByRole("radio", { name: "尽量少绕路" }));
  fireEvent.click(screen.getByRole("button", { name: "确认并开始规划" }));

  await waitFor(() =>
    expect(
      fetchMock.mock.calls.filter((call) => String(call[0]).endsWith("/agent/sessions/sess_stream/messages"))
    ).toHaveLength(1)
  );
  const messageCall = fetchMock.mock.calls.find((call) =>
    String(call[0]).endsWith("/agent/sessions/sess_stream/messages")
  );
  const messageBody = JSON.parse(String(messageCall?.[1]?.body));
  expect(messageBody.context.selectedAgentChoice).toEqual({
    sourceAssistantTurnId: "turn_option_assistant",
    choiceId: "clarification-batch:checkpoint-route-batch",
    batchSelections: [
      { dimensionId: "route_decision.mobility_profile", optionId: "transit" },
      { dimensionId: "route_decision.detour_tolerance", optionId: "less_detour" }
    ]
  });
});

test("single server-signed portfolio continuation is executable without a manual-input option", async () => {
  window.localStorage.removeItem("trip.activeAgentSessionId");
  plannerStore.setState({
    selectedCity: "北京",
    agentSession: null,
    conversationTurns: [],
    itineraryPlan: null,
    planComparison: null,
    preferenceCard: null,
    preferenceMemory: null,
    pendingPoiCandidates: [],
    activeVersionId: null,
    selectedSegmentId: null,
    selectedRouteOptionId: null,
    previewRouteOptionId: null
  });
  const session = agentSessionWithClarificationOptions();
  const assistantTurn = session.turns[1] as unknown as {
    content: string;
    choiceOptions: AgentChoiceOption[];
  };
  assistantTurn.content = "当前规划根仍有未尝试方向，可继续下一个方案方向。";
  assistantTurn.choiceOptions = [
    {
      id: "portfolio_more_plans_root_1",
      index: 1,
      kind: "portfolio_more_plans",
      action: "retry_model_planning",
      label: "继续下一个方案方向",
      lifecycle: "offered",
      planningSelectionRootTurnId: "turn_option_user",
      rootPortfolioId: "portfolio_root_1",
      focusBriefId: "brief_next",
      requestContractFingerprint: "fingerprint_1234567890"
    }
  ];
  const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) return jsonResponse({ mode: "mock", default: [], mock: [] });
    if (path.endsWith("/preferences/memory")) {
      return jsonResponse({
        userId: "test-user",
        memoryText: "# 我的旅行偏好\n",
        autoUpdateEnabled: true,
        createdAt: "2026-06-10T10:00:00Z",
        updatedAt: "2026-06-10T10:00:00Z"
      });
    }
    if (path.endsWith("/preferences/extract")) {
      return jsonResponse({ summaryCard: { ...preferenceCard(""), items: [], summaryText: "" } });
    }
    if (path.endsWith("/agent/sessions/current")) return jsonResponse(session);
    if (path.endsWith("/agent/sessions/sess_stream/messages") && init?.method === "POST") {
      return jsonResponse(agentMessageResponse());
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);

  const planActions = await screen.findByLabelText("方案操作");
  const continueButton = await screen.findByRole("button", { name: /继续下一个方案方向/ });
  expect(planActions.contains(continueButton)).toBe(true);
  expect(screen.queryByText("我自己填写")).toBeNull();
  fireEvent.click(continueButton);

  await waitFor(() =>
    expect(fetchMock.mock.calls.some((call) => String(call[0]).endsWith("/agent/sessions/sess_stream/messages"))).toBe(
      true
    )
  );
  const messageCall = fetchMock.mock.calls.find((call) =>
    String(call[0]).endsWith("/agent/sessions/sess_stream/messages")
  );
  const body = JSON.parse(String(messageCall?.[1]?.body ?? "{}"));
  expect(body.context.selectedAgentChoice).toEqual({
    sourceAssistantTurnId: "turn_option_assistant",
    choiceId: "portfolio_more_plans_root_1"
  });
});

test("a newer planning root disables the older continuation without removing its comparison card", async () => {
  window.localStorage.removeItem("trip.activeAgentSessionId");
  const createdAt = "2026-08-06T09:00:00Z";
  const earlierProjection = {
    ...comparisonStreamProjection("proposal_old_root", "旧规划方案"),
    planningSelectionRootTurnId: "root_old",
    rootPortfolioId: "portfolio_old",
    sourceAssistantTurnId: "assistant_old",
    choiceId: "choice_old_plan"
  };
  const currentProjection = {
    ...comparisonStreamProjection("proposal_current_root", "新规划方案"),
    planningSelectionRootTurnId: "root_current",
    rootPortfolioId: "portfolio_current",
    sourceAssistantTurnId: "assistant_current",
    choiceId: "choice_current_plan"
  };
  const assistantTurn = (
    id: string,
    turnIndex: number,
    projection: typeof earlierProjection,
    continuationId: string,
    continuationLabel: string,
    includeExplicitScope = true
  ): AgentSession["turns"][number] => ({
    id,
    role: "assistant" as const,
    content: continuationLabel,
    turnIndex,
    status: "active" as const,
    comparisonProjectionUpdateMode: "replace" as const,
    comparisonProjections: [projection],
    choiceOptions: [
      {
        id: projection.choiceId,
        kind: "portfolio_comparison_readonly",
        label: projection.title,
        comparisonProjection: projection
      },
      {
        id: continuationId,
        kind: "portfolio_more_plans",
        action: "retry_model_planning",
        label: continuationLabel,
        lifecycle: "offered",
        ...(includeExplicitScope
          ? {
              planningSelectionRootTurnId: projection.planningSelectionRootTurnId,
              rootPortfolioId: projection.rootPortfolioId
            }
          : {})
      }
    ],
    planningSteps: [],
    toolEvents: [],
    createdAt,
    updatedAt: createdAt
  });
  const session: AgentSession = {
    ...agentSession(),
    turns: [
      {
        id: "user_old",
        role: "user",
        content: "第一次规划",
        turnIndex: 1,
        status: "active",
        createdAt,
        updatedAt: createdAt
      },
      assistantTurn("assistant_old", 2, earlierProjection, "continue_old", "继续旧规划", false),
      {
        id: "user_current",
        role: "user",
        content: "第二次规划",
        turnIndex: 3,
        status: "active",
        createdAt,
        updatedAt: createdAt
      },
      assistantTurn("assistant_current", 4, currentProjection, "continue_current", "继续新规划")
    ]
  };
  const fetchMock = vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) return jsonResponse({ mode: "mock", default: [], mock: [] });
    if (path.endsWith("/preferences/extract")) return jsonResponse({ summaryCard: preferenceCard("") });
    if (path.endsWith("/agent/sessions/current")) return jsonResponse(session);
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);

  const oldContinuation = await screen.findByRole("button", { name: /继续旧规划/ });
  const currentContinuation = await screen.findByRole("button", { name: /继续新规划/ });
  expect((oldContinuation as HTMLButtonElement).disabled).toBe(true);
  expect(oldContinuation.textContent).toContain("已由后续规划请求取代");
  expect((currentContinuation as HTMLButtonElement).disabled).toBe(false);
  expect(plannerStore.getSnapshot().comparisonPreview.plans.map((plan) => plan.proposalId)).toEqual([
    "proposal_old_root",
    "proposal_current_root"
  ]);
  expect(fetchMock.mock.calls.some((call) => String(call[0]).includes("/messages"))).toBe(false);
});

test("single ordinary clarification is not promoted to an opaque executable action", async () => {
  window.localStorage.removeItem("trip.activeAgentSessionId");
  plannerStore.setState({
    selectedCity: "北京",
    agentSession: null,
    conversationTurns: [],
    itineraryPlan: null,
    planComparison: null,
    preferenceCard: null,
    preferenceMemory: null,
    pendingPoiCandidates: [],
    activeVersionId: null,
    selectedSegmentId: null,
    selectedRouteOptionId: null,
    previewRouteOptionId: null
  });
  const session = agentSessionWithClarificationOptions();
  session.turns[1].content = "还需要确认预算。";
  session.turns[1].choiceOptions = [{ index: 1, label: "中等预算", value: "中等预算", kind: "preset" }];
  const fetchMock = vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) return jsonResponse({ mode: "mock", default: [], mock: [] });
    if (path.endsWith("/preferences/memory")) {
      return jsonResponse({
        userId: "test-user",
        memoryText: "# 我的旅行偏好\n",
        autoUpdateEnabled: true,
        createdAt: "2026-06-10T10:00:00Z",
        updatedAt: "2026-06-10T10:00:00Z"
      });
    }
    if (path.endsWith("/agent/sessions/current")) return jsonResponse(session);
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);

  await screen.findByText("还需要确认预算。");
  expect(screen.queryByRole("button", { name: "中等预算" })).toBeNull();
  expect(screen.queryByText("我自己填写")).toBeNull();
});

test("visible executable choices are renumbered after readonly carriers are filtered", async () => {
  window.localStorage.removeItem("trip.activeAgentSessionId");
  plannerStore.setState({
    selectedCity: "北京",
    agentSession: null,
    conversationTurns: [],
    itineraryPlan: null,
    planComparison: null,
    preferenceCard: null,
    preferenceMemory: null,
    pendingPoiCandidates: [],
    activeVersionId: null,
    selectedSegmentId: null,
    selectedRouteOptionId: null,
    previewRouteOptionId: null
  });
  const session = agentSessionWithClarificationOptions();
  session.turns[1].content = "当前规划根仍有两个可执行操作。";
  session.turns[1].choiceOptions = [
    { id: "readonly_projection_1", index: 1, kind: "portfolio_comparison_readonly", label: "方案投影" },
    {
      id: "portfolio_more_plans_root_1",
      index: 2,
      kind: "portfolio_more_plans",
      action: "retry_model_planning",
      label: "继续生成其他方案",
      lifecycle: "offered"
    },
    { id: "readonly_projection_2", index: 3, kind: "portfolio_comparison_readonly", label: "历史方案投影" },
    {
      id: "portfolio_complete_partial_root_1",
      index: 4,
      kind: "portfolio_complete_partial",
      action: "retry_model_planning",
      label: "继续生成其他方案",
      lifecycle: "offered"
    }
  ] as AgentChoiceOption[];
  const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) return jsonResponse({ mode: "mock", default: [], mock: [] });
    if (path.endsWith("/preferences/memory")) {
      return jsonResponse({
        userId: "test-user",
        memoryText: "# 我的旅行偏好\n",
        autoUpdateEnabled: true,
        createdAt: "2026-06-10T10:00:00Z",
        updatedAt: "2026-06-10T10:00:00Z"
      });
    }
    if (path.endsWith("/agent/sessions/current")) return jsonResponse(session);
    if (path.endsWith("/agent/sessions/sess_stream/messages") && init?.method === "POST") {
      return jsonResponse(agentMessageResponse());
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);

  const visibleContinuationButtons = await screen.findAllByRole("button", { name: /继续生成其他方案/ });
  expect(visibleContinuationButtons).toHaveLength(2);
  const [first, second] = visibleContinuationButtons;
  expect(first.textContent).toMatch(/^1\s*继续生成其他方案/);
  expect(second.textContent).toMatch(/^2\s*继续生成其他方案/);
  expect(screen.queryByText("方案投影")).toBeNull();
  fireEvent.click(second);

  await waitFor(() =>
    expect(fetchMock.mock.calls.some((call) => String(call[0]).endsWith("/agent/sessions/sess_stream/messages"))).toBe(
      true
    )
  );
  expect(first).toBeTruthy();
  const messageCall = fetchMock.mock.calls.find((call) =>
    String(call[0]).endsWith("/agent/sessions/sess_stream/messages")
  );
  const body = JSON.parse(String(messageCall?.[1]?.body ?? "{}"));
  expect(body.context.selectedAgentChoice).toEqual({
    sourceAssistantTurnId: "turn_option_assistant",
    choiceId: "portfolio_complete_partial_root_1"
  });
});

test("safe fallback choice shows executing then consumed and skips synthetic preference extraction", async () => {
  const pending = deferred<Response>();
  let preferenceExtractionCount = 0;
  const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) {
      return jsonResponse({ mode: "mock", default: [], mock: [] });
    }
    if (path.endsWith("/preferences/extract")) {
      preferenceExtractionCount += 1;
      return jsonResponse({ summaryCard: preferenceCard("") });
    }
    if (path.endsWith("/agent/sessions/current")) {
      return jsonResponse(agentSessionWithSafeFallbackOptions());
    }
    if (path.endsWith("/agent/sessions/sess_stream/messages") && init?.method === "POST") {
      return pending.promise;
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);
  const ruleButton = await screen.findByRole("button", { name: /使用规则安全草稿/ });
  fireEvent.click(ruleButton);

  await waitFor(() =>
    expect((screen.getByRole("button", { name: /使用规则安全草稿（执行中）/ }) as HTMLButtonElement).disabled).toBe(
      true
    )
  );
  expect(preferenceExtractionCount).toBe(0);
  const messageCall = fetchMock.mock.calls.find((call) =>
    String(call[0]).endsWith("/agent/sessions/sess_stream/messages")
  );
  const body = JSON.parse(String(messageCall?.[1]?.body ?? "{}"));
  expect(body.context.selectedAgentChoice).toEqual({
    sourceAssistantTurnId: "turn_fallback_assistant",
    choiceId: "fallback:confirm_rule_safe_draft:decision_1"
  });
  expect(body.context.selectedAgentChoice.choiceId).not.toBe("fallback:retry_model_planning:decision_1");

  const completed = agentMessageResponse();
  completed.userTurn = {
    ...completed.userTurn,
    structuredChoiceTrace: {
      sourceAssistantTurnId: "turn_fallback_assistant",
      resolvedChoiceId: "fallback:confirm_rule_safe_draft:decision_1",
      executionStatus: "succeeded"
    }
  };
  pending.resolve(jsonResponse(completed));
  await waitFor(() =>
    expect((screen.getByRole("button", { name: /使用规则安全草稿（已处理）/ }) as HTMLButtonElement).disabled).toBe(
      true
    )
  );
});

test("explicit allowsManualInput false overrides legacy custom kind and label", async () => {
  const session = agentSessionWithSafeFallbackOptions() as AgentSession;
  session.turns[1].choiceOptions = [
    {
      id: "structured_route_preference",
      index: 1,
      kind: "custom_input",
      action: "manual_continuation",
      label: "我直接说明绕路偏好",
      lifecycle: "offered",
      allowsManualInput: false
    }
  ];
  const fetchMock = vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) return jsonResponse({ mode: "mock", default: [], mock: [] });
    if (path.endsWith("/preferences/memory")) {
      return jsonResponse({
        userId: "test-user",
        memoryText: "",
        autoUpdateEnabled: true,
        createdAt: "2026-06-10T10:00:00Z",
        updatedAt: "2026-06-10T10:00:00Z"
      });
    }
    if (path.endsWith("/agent/sessions/current")) return jsonResponse(session);
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);

  expect(await screen.findByRole("button", { name: /我直接说明绕路偏好/ })).toBeTruthy();
  expect(screen.queryByRole("textbox", { name: "自填选项内容" })).toBeNull();
});

test("safe fallback manual continuation honors allowsManualInput and preserves opaque identity", async () => {
  const session = agentSessionWithSafeFallbackOptions() as AgentSession;
  session.turns[1].choiceOptions = [
    {
      id: "manual_route_preference",
      index: 1,
      kind: "safe_fallback_action",
      action: "manual_continuation",
      label: "我直接说明绕路偏好",
      lifecycle: "offered",
      allowsManualInput: true
    }
  ];
  const requestBodies: Array<Record<string, unknown>> = [];
  const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) return jsonResponse({ mode: "mock", default: [], mock: [] });
    if (path.endsWith("/preferences/memory")) {
      return jsonResponse({
        userId: "test-user",
        memoryText: "",
        autoUpdateEnabled: true,
        createdAt: "2026-06-10T10:00:00Z",
        updatedAt: "2026-06-10T10:00:00Z"
      });
    }
    if (path.endsWith("/preferences/extract")) {
      return jsonResponse({ summaryCard: { ...preferenceCard(""), items: [], summaryText: "" } });
    }
    if (path.endsWith("/agent/sessions/current")) return jsonResponse(session);
    if (path.endsWith("/agent/sessions/sess_stream/messages") && init?.method === "POST") {
      requestBodies.push(JSON.parse(String(init.body ?? "{}")) as Record<string, unknown>);
      return jsonResponse(agentMessageResponse());
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);

  const manualInput = await screen.findByRole("textbox", { name: "自填选项内容" });
  fireEvent.change(manualInput, { target: { value: "公共交通为主，最多接受 15% 绕行" } });
  const sendButton = screen.getByRole("button", { name: "发送" });
  fireEvent.click(sendButton);

  await waitFor(() => expect(requestBodies).toHaveLength(1));
  expect(requestBodies[0].content).toBe("公共交通为主，最多接受 15% 绕行");
  expect((requestBodies[0].context as Record<string, unknown>).selectedAgentChoice).toEqual({
    sourceAssistantTurnId: "turn_fallback_assistant",
    choiceId: "manual_route_preference",
    manualValue: "公共交通为主，最多接受 15% 绕行"
  });
});

test("text-only legacy options do not create submit-capable inferred identities", async () => {
  const session = agentSessionWithSafeFallbackOptions() as AgentSession;
  session.turns[1].content = "请选择一个路线偏好：\n1. 尽量少绕路\n2. 可以接受少量绕行\n3. 我自己填写";
  session.turns[1].choiceOptions = undefined;
  const fetchMock = vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) return jsonResponse({ mode: "mock", default: [], mock: [] });
    if (path.endsWith("/preferences/memory")) {
      return jsonResponse({
        userId: "test-user",
        memoryText: "",
        autoUpdateEnabled: true,
        createdAt: "2026-06-10T10:00:00Z",
        updatedAt: "2026-06-10T10:00:00Z"
      });
    }
    if (path.endsWith("/agent/sessions/current")) return jsonResponse(session);
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);

  await screen.findByText(/请选择一个路线偏好/);
  expect(screen.queryByRole("button", { name: /尽量少绕路/ })).toBeNull();
  expect(screen.queryByRole("button", { name: /可以接受少量绕行/ })).toBeNull();
  expect(screen.queryByRole("textbox", { name: "自填选项内容" })).toBeNull();
});

test("server choices without a manual choice id do not synthesize a custom input", async () => {
  const session = agentSessionWithSafeFallbackOptions() as AgentSession;
  session.turns[1].content = "请选择一个服务端提供的路线偏好。";
  session.turns[1].choiceOptions = [
    { id: "route_short", index: 1, kind: "route_preference", label: "尽量少绕路", lifecycle: "offered" },
    { id: "route_flexible", index: 2, kind: "route_preference", label: "可以接受少量绕行", lifecycle: "offered" }
  ];
  const fetchMock = vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) return jsonResponse({ mode: "mock", default: [], mock: [] });
    if (path.endsWith("/preferences/memory")) {
      return jsonResponse({
        userId: "test-user",
        memoryText: "",
        autoUpdateEnabled: true,
        createdAt: "2026-06-10T10:00:00Z",
        updatedAt: "2026-06-10T10:00:00Z"
      });
    }
    if (path.endsWith("/agent/sessions/current")) return jsonResponse(session);
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);

  expect(await screen.findByRole("button", { name: /尽量少绕路/ })).toBeTruthy();
  expect(screen.getByRole("button", { name: /可以接受少量绕行/ })).toBeTruthy();
  expect(screen.queryByRole("textbox", { name: "自填选项内容" })).toBeNull();
  expect(screen.queryByText("我自己填写")).toBeNull();
});

test("session reload keeps the reconciled direction choice disabled and the new continuation available", async () => {
  const session = agentSessionWithSafeFallbackOptions() as unknown as AgentSession;
  const baseTurn = session.turns[1];
  session.turns = [
    session.turns[0],
    {
      ...baseTurn,
      id: "turn_direction_old",
      content: "本页候选已检查，下一次可继续探索。",
      choiceOptions: [
        {
          id: "choice_direction_old",
          index: 1,
          kind: "simple_direction_more_plans",
          action: "continue_plan_expansion",
          label: "继续探索其他方向",
          value: "继续探索其他方向",
          lifecycle: "consumed",
          sourceAssistantTurnId: "turn_direction_old",
          planningSelectionRootTurnId: "turn_direction_root",
          rootPortfolioId: "portfolio_direction_root",
          requestContractFingerprint: "f".repeat(64)
        }
      ] as AgentChoiceOption[]
    },
    {
      ...session.turns[0],
      id: "turn_direction_request",
      content: "继续探索其他方向",
      turnIndex: 3
    },
    {
      ...baseTurn,
      id: "turn_direction_new",
      content: "已检查上一页候选，可以从后续候选继续。",
      turnIndex: 4,
      choiceOptions: [
        {
          id: "choice_direction_new",
          index: 1,
          kind: "simple_direction_more_plans",
          action: "continue_plan_expansion",
          label: "继续探索其他方向",
          value: "继续探索其他方向",
          lifecycle: "offered",
          sourceAssistantTurnId: "turn_direction_new",
          planningSelectionRootTurnId: "turn_direction_root",
          rootPortfolioId: "portfolio_direction_root",
          requestContractFingerprint: "f".repeat(64)
        }
      ] as AgentChoiceOption[]
    }
  ];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: RequestInfo | URL) => {
      const path = String(url);
      if (path.endsWith("/providers/status")) return jsonResponse({ mode: "mock", default: [], mock: [] });
      if (path.endsWith("/preferences/extract")) return jsonResponse({ summaryCard: preferenceCard("") });
      if (path.endsWith("/agent/sessions/current")) return jsonResponse(session);
      return jsonResponse({}, 404);
    })
  );

  render(<AppShell />);

  const consumed = await screen.findByRole("button", { name: /继续探索其他方向（已处理）/ });
  const offered = screen.getByRole("button", { name: /继续探索其他方向$/ });
  expect((consumed as HTMLButtonElement).disabled).toBe(true);
  expect((offered as HTMLButtonElement).disabled).toBe(false);
});

test("failed retryable structured choice restores the button without opening POI selection", async () => {
  const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) return jsonResponse({ mode: "mock", default: [], mock: [] });
    if (path.endsWith("/preferences/extract")) return jsonResponse({ summaryCard: preferenceCard("") });
    if (path.endsWith("/agent/sessions/current")) return jsonResponse(agentSessionWithSafeFallbackOptions());
    if (path.endsWith("/agent/sessions/sess_stream/messages") && init?.method === "POST") {
      const response = agentMessageResponse();
      response.userTurn = {
        ...response.userTurn,
        structuredChoiceTrace: {
          sourceAssistantTurnId: "turn_fallback_assistant",
          resolvedChoiceId: "fallback:confirm_rule_safe_draft:decision_1",
          executionStatus: "failed_retryable"
        }
      };
      return jsonResponse(response);
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);
  fireEvent.click(await screen.findByRole("button", { name: /使用规则安全草稿/ }));

  await waitFor(() => {
    const retryButton = screen.getByRole("button", { name: /使用规则安全草稿（可重试）/ }) as HTMLButtonElement;
    expect(retryButton.disabled).toBe(false);
  });
});

test("open density map compares one scoped candidate group without calling Agent", async () => {
  const session = agentSessionWithSafeFallbackOptions();
  (
    session as unknown as {
      pendingPoiCandidates: Array<Record<string, unknown>>;
    }
  ).pendingPoiCandidates = [
    {
      id: "cand_day_2_walk",
      query: "Day 2 街区漫步",
      city: "北京",
      category: "area_walk",
      status: "pending",
      candidates: [
        {
          id: "B0SHICHAHAI",
          amapId: "B0SHICHAHAI",
          name: "什刹海",
          type: "风景名胜",
          city: "北京",
          district: "西城区",
          address: "什刹海街道",
          longitude: 116.389,
          latitude: 39.941,
          category: "scenic",
          source: "amap_web_service",
          sourceNote: "高德 WebService POI 搜索",
          confidence: 0.94,
          photos: []
        }
      ],
      createdAt: "2026-07-21T08:00:00Z"
    }
  ];
  const densityTurn = session.turns[1] as unknown as { choiceOptions: AgentChoiceOption[] };
  densityTurn.choiceOptions = [
    {
      id: "density_day_2_shichahai",
      index: 1,
      kind: "portfolio_density_candidate",
      action: "resume_density_candidate",
      label: "Day 2：什刹海",
      value: "Day 2：什刹海",
      lifecycle: "offered",
      selectionGroupId: "cand_day_2_walk",
      candidateRecordId: "cand_day_2_walk",
      amapId: "B0SHICHAHAI",
      briefId: "brief_day_2",
      poolId: "pool_day_2_walk",
      planningSlotId: "slot_day_2",
      dayNumber: 2
    },
    {
      id: "portfolio_density_map_1",
      index: 2,
      kind: "portfolio_density_map",
      action: "open_density_map",
      label: "在地图查看 Day 2 缺失地点",
      value: "在地图查看 Day 2 缺失地点",
      lifecycle: "offered",
      dayNumber: 2,
      planningSlotId: "slot_day_2",
      candidateRecordId: "cand_day_2_walk",
      selectionGroupId: "cand_day_2_walk",
      briefId: "brief_day_2",
      poolId: "pool_day_2_walk",
      timeWindow: "14:00-16:00",
      displayNeed: "街区漫步",
      comparisonAnchors: [
        {
          id: "B0CAMPUS",
          amapId: "B0CAMPUS",
          name: "北京大学",
          type: "科教文化服务;高等院校",
          city: "北京",
          district: "海淀区",
          address: "颐和园路5号",
          longitude: 116.31,
          latitude: 39.99,
          category: "university",
          source: "amap_web_service",
          sourceNote: "高德 WebService POI 搜索",
          confidence: 0.94,
          photos: [],
          startTime: "09:00"
        }
      ]
    },
    {
      id: "portfolio_density_manual_1",
      index: 2,
      kind: "custom_input",
      action: "manual_continuation",
      label: "我自己填写",
      value: "我自己填写",
      lifecycle: "offered"
    }
  ];
  const fetchMock = vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) return jsonResponse({ mode: "mock", default: [], mock: [] });
    if (path.endsWith("/preferences/extract")) return jsonResponse({ summaryCard: preferenceCard("") });
    if (path.endsWith("/agent/sessions/current")) return jsonResponse(session);
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);
  fireEvent.click(await screen.findByRole("button", { name: /在地图查看 Day 2 缺失地点/ }));

  expect(plannerStore.getSnapshot().selectedDayNumber).toBe(2);
  expect(plannerStore.getSnapshot().activeDensityMapComparison).toMatchObject({
    candidateRecordId: "cand_day_2_walk",
    briefId: "brief_day_2",
    poolId: "pool_day_2_walk",
    dayNumber: 2,
    planningSlotId: "slot_day_2",
    timeWindow: "14:00-16:00",
    displayNeed: "街区漫步"
  });
  expect(plannerStore.getSnapshot().activeDensityMapComparison?.anchors[0].name).toBe("北京大学");
  expect(plannerStore.getSnapshot().activeDensityMapComparison?.candidateChoices).toEqual([
    {
      amapId: "B0SHICHAHAI",
      sourceAssistantTurnId: session.turns[1].id,
      choiceId: "density_day_2_shichahai",
      label: "Day 2：什刹海"
    }
  ]);
  expect(screen.getByLabelText("候选位置对比模式").textContent).toContain("Day 2 · 14:00-16:00 · 街区漫步");
  expect(screen.queryByText(/点击标点加入某天行程/)).toBeNull();
  expect(fetchMock.mock.calls.some((call) => String(call[0]).includes("/messages"))).toBe(false);
});

test("portfolio comparison hydration does not auto-open generic POI selection", async () => {
  const projection = comparisonStreamProjection("proposal_existing", "已生成方案");
  const session = {
    ...agentSession(),
    pendingPoiCandidates: [
      {
        id: "candidate_from_partial",
        query: "北京特色午餐",
        city: "北京",
        category: "food",
        status: "pending",
        candidates: [],
        createdAt: "2026-07-30T09:40:00Z"
      }
    ],
    turns: [
      {
        id: "assistant_expansion",
        role: "assistant",
        content: "已在行程对比中追加 1 个新方案。",
        turnIndex: 1,
        status: "active",
        comparisonProjectionUpdateMode: "append",
        comparisonProjections: [
          {
            ...projection,
            sourceAssistantTurnId: "assistant_expansion"
          }
        ],
        choiceOptions: [
          {
            id: projection.choiceId,
            kind: "plan_proposal",
            action: "select_plan_proposal",
            label: projection.title,
            comparisonProjection: {
              ...projection,
              sourceAssistantTurnId: "assistant_expansion"
            }
          },
          {
            id: "choice_more_plans",
            kind: "portfolio_more_plans",
            action: "retry_model_planning",
            label: "继续生成其他方案"
          }
        ],
        planningSteps: [],
        toolEvents: [],
        createdAt: "2026-07-30T09:40:00Z",
        updatedAt: "2026-07-30T09:40:00Z"
      }
    ]
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: RequestInfo | URL) => {
      const path = String(url);
      if (path.endsWith("/providers/status")) {
        return jsonResponse({ mode: "mock", default: [], mock: [] });
      }
      if (path.endsWith("/preferences/extract")) {
        return jsonResponse({ summaryCard: preferenceCard("") });
      }
      if (path.endsWith("/agent/sessions/current")) {
        return jsonResponse(session);
      }
      return jsonResponse({}, 404);
    })
  );

  render(<AppShell />);

  await screen.findByText("已在行程对比中追加 1 个新方案。");
  expect(screen.queryByText(/正在选择 POI/)).toBeNull();
  expect(screen.queryByText(/点击标点加入某天行程/)).toBeNull();
});

test("renders opaque candidate choices from every missing density slot", async () => {
  const session = agentSessionWithSafeFallbackOptions();
  const densityTurn = session.turns[1] as unknown as { choiceOptions: AgentChoiceOption[] };
  densityTurn.choiceOptions = [
    {
      id: "density_day_1_shichahai",
      index: 1,
      kind: "portfolio_density_candidate",
      action: "resume_density_candidate",
      label: "Day 1：什刹海",
      value: "Day 1：什刹海",
      lifecycle: "offered",
      selectionGroupId: "cand_day_1_walk",
      candidateRecordId: "cand_day_1_walk",
      amapId: "B0SHICHAHAI",
      briefId: "brief_focus",
      poolId: "pool_day_1_walk",
      planningSlotId: "slot_day_1_walk",
      dayNumber: 1
    },
    {
      id: "density_day_2_olympic",
      index: 2,
      kind: "portfolio_density_candidate",
      action: "resume_density_candidate",
      label: "Day 2：奥林匹克森林公园",
      value: "Day 2：奥林匹克森林公园",
      lifecycle: "offered",
      selectionGroupId: "cand_day_2_night",
      candidateRecordId: "cand_day_2_night",
      amapId: "B0OLYMPIC",
      briefId: "brief_focus",
      poolId: "pool_day_2_night",
      planningSlotId: "slot_day_2_night",
      dayNumber: 2
    },
    {
      id: "density_day_1_refresh",
      index: 3,
      kind: "portfolio_density_retry",
      action: "refresh_density_candidates",
      label: "刷新 Day 1 街区漫步候选",
      lifecycle: "offered",
      selectionGroupId: "cand_day_1_walk",
      briefId: "brief_focus",
      poolId: "pool_day_1_walk",
      planningSlotId: "slot_day_1_walk",
      dayNumber: 1
    },
    {
      id: "density_day_2_map",
      index: 4,
      kind: "portfolio_density_map",
      action: "open_density_map",
      label: "在地图对比 Day 2 候选位置",
      lifecycle: "offered",
      selectionGroupId: "cand_day_2_night",
      candidateRecordId: "cand_day_2_night",
      briefId: "brief_focus",
      poolId: "pool_day_2_night",
      planningSlotId: "slot_day_2_night",
      dayNumber: 2
    },
    {
      id: "density_day_2_manual",
      index: 5,
      kind: "custom_input",
      action: "manual_continuation",
      label: "手动输入 Day 2 夜景地点",
      lifecycle: "offered",
      briefId: "brief_focus",
      poolId: "pool_day_2_night",
      planningSlotId: "slot_day_2_night",
      dayNumber: 2
    }
  ] as AgentChoiceOption[];
  const fetchMock = vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) return jsonResponse({ mode: "mock", default: [], mock: [] });
    if (path.endsWith("/preferences/extract")) return jsonResponse({ summaryCard: preferenceCard("") });
    if (path.endsWith("/agent/sessions/current")) return jsonResponse(session);
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);

  expect(await screen.findByRole("button", { name: /Day 1：什刹海/ })).toBeTruthy();
  expect(screen.getByRole("button", { name: /Day 2：奥林匹克森林公园/ })).toBeTruthy();
  expect(screen.getByRole("status").textContent).toContain("点击一个真实地图候选确认并写入时间轴");
  expect(screen.getByRole("button", { name: "搜索候选" })).toBeTruthy();
});

test("open density map fails closed when its persisted scope is incomplete", async () => {
  const session = agentSessionWithSafeFallbackOptions();
  const densityTurn = session.turns[1] as unknown as {
    choiceOptions: AgentChoiceOption[];
  };
  densityTurn.choiceOptions = [
    {
      id: "portfolio_density_map_incomplete",
      index: 1,
      kind: "portfolio_density_map",
      action: "open_density_map",
      label: "在地图对比缺失候选",
      value: "在地图对比缺失候选",
      lifecycle: "offered",
      dayNumber: 1,
      planningSlotId: "slot_day_1"
    },
    {
      id: "portfolio_density_manual_incomplete",
      index: 2,
      kind: "custom_input",
      action: "manual_continuation",
      label: "我自己填写",
      value: "我自己填写",
      lifecycle: "offered"
    }
  ];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: RequestInfo | URL) => {
      const path = String(url);
      if (path.endsWith("/providers/status")) {
        return jsonResponse({ mode: "mock", default: [], mock: [] });
      }
      if (path.endsWith("/preferences/extract")) {
        return jsonResponse({ summaryCard: preferenceCard("") });
      }
      if (path.endsWith("/agent/sessions/current")) {
        return jsonResponse(session);
      }
      return jsonResponse({}, 404);
    })
  );

  render(<AppShell />);
  fireEvent.click(
    await screen.findByRole("button", {
      name: /在地图对比缺失候选/
    })
  );

  expect(plannerStore.getSnapshot().activeDensityMapComparison).toBeNull();
});

test("structured POI choice sends its opaque identity to the authoritative Agent dispatcher", async () => {
  plannerStore.setState({
    selectedCity: "北京",
    agentSession: null,
    conversationTurns: [],
    itineraryPlan: null,
    planComparison: null,
    preferenceCard: null,
    preferenceMemory: null,
    pendingPoiCandidates: [],
    activeVersionId: null,
    selectedSegmentId: null,
    selectedRouteOptionId: null,
    previewRouteOptionId: null
  });
  const session = agentSessionWithStructuredPoiChoice();
  const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) {
      return jsonResponse({ mode: "mock", default: [], mock: [] });
    }
    if (path.endsWith("/preferences/memory")) {
      return jsonResponse({
        userId: "test-user",
        memoryText: "",
        autoUpdateEnabled: true,
        createdAt: "2026-06-10T10:00:00Z",
        updatedAt: "2026-06-10T10:00:00Z"
      });
    }
    if (path.endsWith("/preferences/extract")) {
      return jsonResponse({ summaryCard: { ...preferenceCard(""), items: [], summaryText: "" } });
    }
    if (path.endsWith("/agent/sessions/current")) {
      return jsonResponse(session);
    }
    if (path.endsWith("/agent/sessions/sess_stale/messages/stream") && init?.method === "POST") {
      return jsonResponse({}, 404);
    }
    if (path.endsWith("/agent/sessions/sess_stale/messages") && init?.method === "POST") {
      return jsonResponse({
        ...agentMessageResponse(),
        itinerary: session.itinerary,
        version: { id: "ver_structured", versionNumber: 2, sourceType: "agent" }
      });
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);

  expect(await screen.findAllByRole("button", { name: /清华大学清芬园|清华大学紫荆园|清华大学桃李园/ })).toHaveLength(
    3
  );
  expect(screen.getByText("我自己填写具体地点")).toBeTruthy();
  expect(screen.queryByText("中国美术馆")).toBeNull();
  const optionButton = screen.getByRole("button", { name: /清华大学紫荆园/ });
  fireEvent.click(optionButton);
  await waitFor(() => {
    const messageCalls = fetchMock.mock.calls.filter((call) =>
      String(call[0]).endsWith("/agent/sessions/sess_stale/messages")
    );
    expect(messageCalls).toHaveLength(1);
  });
  const messageCall = fetchMock.mock.calls.find((call) =>
    String(call[0]).endsWith("/agent/sessions/sess_stale/messages")
  );
  const body = JSON.parse(String(messageCall?.[1]?.body ?? "{}"));
  expect(body.content).toBe("清华大学紫荆园");
  expect(body.context.selectedAgentChoice).toEqual({
    sourceAssistantTurnId: "turn_structured_assistant",
    choiceId: "choice_2"
  });
  expect(fetchMock.mock.calls.some((call) => String(call[0]).endsWith("/itineraries/plan_stale/patch"))).toBe(false);
  await waitFor(() => expect(plannerStore.getSnapshot().activeVersionId).toBe("ver_structured"));
});

test.each([
  [
    409,
    { code: "agent_choice_target_stale", message: "该候选对应的日程段已变化。" },
    "ver_latest",
    "该候选或行程版本已变化，已恢复当前会话的最新候选，请重新选择。"
  ],
  [
    409,
    {
      code: "portfolio_partial_timeline_missing",
      message: "当前没有可安全补槽的部分时间轴，请先恢复上次规划检查点。"
    },
    "ver_latest_partial",
    "该候选或行程版本已变化，已恢复当前会话的最新候选，请重新选择。"
  ],
  [
    422,
    { code: "agent_choice_invalid", message: "该候选地点已失效。" },
    "ver_latest_candidates",
    "该候选或行程版本已变化，已恢复当前会话的最新候选，请重新选择。"
  ],
  [
    409,
    { code: "plan_proposal_request_scope_invalid", message: "该方案采用能力不属于当前请求合同。" },
    "ver_latest_proposal",
    "方案确认入口已更新，已加载当前会话的最新方案，请重新点击确认。"
  ]
])(
  "server choice dispatcher rejects stale or invalid choice with a session refresh (%i)",
  async (status, detail, versionId, expectedMessage) => {
    plannerStore.setState({
      selectedCity: "北京",
      agentSession: null,
      conversationTurns: [],
      itineraryPlan: null,
      activeVersionId: null,
      pendingPoiCandidates: []
    });
    const session = agentSessionWithStructuredPoiChoice();
    const latest = { ...session, activeVersionId: versionId, title: "已刷新候选" };
    const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      const path = String(url);
      if (path.endsWith("/providers/status")) return jsonResponse({ mode: "mock", default: [], mock: [] });
      if (path.endsWith("/preferences/memory"))
        return jsonResponse({ userId: "test-user", memoryText: "", autoUpdateEnabled: true });
      if (path.endsWith("/preferences/extract"))
        return jsonResponse({ summaryCard: { ...preferenceCard(""), items: [], summaryText: "" } });
      if (path.endsWith("/agent/sessions/current")) return jsonResponse(session);
      if (path.endsWith("/agent/sessions/sess_stale")) return jsonResponse(latest);
      if (path.endsWith("/agent/sessions/sess_stale/messages/stream") && init?.method === "POST")
        return jsonResponse({}, 404);
      if (path.endsWith("/agent/sessions/sess_stale/messages") && init?.method === "POST")
        return jsonResponse({ detail }, status);
      return jsonResponse({}, 404);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<AppShell />);

    const optionButton = await screen.findByRole("button", { name: /清华大学紫荆园/ });
    const exactSessionCallsBeforeClick = fetchMock.mock.calls.filter((call) =>
      String(call[0]).endsWith("/agent/sessions/sess_stale")
    ).length;
    fireEvent.click(optionButton);

    expect(await screen.findByText(expectedMessage)).toBeTruthy();
    await waitFor(() => expect(plannerStore.getSnapshot().activeVersionId).toBe(versionId));
    expect(fetchMock.mock.calls.filter((call) => String(call[0]).endsWith("/agent/sessions/sess_stale"))).toHaveLength(
      exactSessionCallsBeforeClick + 1
    );
    expect(fetchMock.mock.calls.some((call) => String(call[0]).endsWith("/itineraries/plan_stale/patch"))).toBe(false);
  }
);

test("generic verifier route-quality failure is not mislabeled as schema invalid", async () => {
  plannerStore.setState({
    selectedCity: "北京",
    agentSession: null,
    conversationTurns: [],
    itineraryPlan: null,
    planComparison: null,
    preferenceCard: null,
    preferenceMemory: null,
    pendingPoiCandidates: [],
    activeVersionId: null,
    selectedSegmentId: null,
    selectedRouteOptionId: null,
    previewRouteOptionId: null
  });
  const session = agentSessionWithStructuredPoiChoice();
  const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) return jsonResponse({ mode: "mock", default: [], mock: [] });
    if (path.endsWith("/preferences/memory"))
      return jsonResponse({ userId: "test-user", memoryText: "", autoUpdateEnabled: true });
    if (path.endsWith("/preferences/extract"))
      return jsonResponse({ summaryCard: { ...preferenceCard(""), items: [], summaryText: "" } });
    if (path.endsWith("/agent/sessions/current")) return jsonResponse(session);
    if (path.endsWith("/agent/sessions/sess_stale/messages/stream") && init?.method === "POST") {
      return agentExecutionErrorStreamResponse("所选方案中的餐饮与相邻路线偏绕，未创建正式行程。", 409, {
        code: "AGENT_VERIFIER_FAILED",
        details: {
          hardFailures: ["route_quality: meal_detour_high"],
          routeQualityIssues: [
            {
              fromPoiName: "清华大学",
              toPoiName: "老北京炸酱面",
              distanceKm: 15,
              durationMinutes: 69
            }
          ],
          recommendedNextActions: ["choose_nearby_meal"]
        }
      });
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);
  expect(await screen.findByRole("button", { name: /清华大学紫荆园/ })).toBeTruthy();
  const exactSessionCallsBeforeClick = fetchMock.mock.calls.filter((call) =>
    String(call[0]).endsWith("/agent/sessions/sess_stale")
  ).length;

  fireEvent.click(screen.getByRole("button", { name: /清华大学紫荆园/ }));

  expect(await screen.findByText(/清华大学.*老北京炸酱面.*15.*69/)).toBeTruthy();
  expect(screen.getByRole("button", { name: /清华大学紫荆园/ })).toBeTruthy();
  expect(fetchMock.mock.calls.filter((call) => String(call[0]).endsWith("/agent/sessions/sess_stale"))).toHaveLength(
    exactSessionCallsBeforeClick
  );
});
test("exact pending-slot manual input sends Beijing Zoo with persisted opaque identity", async () => {
  plannerStore.setState({
    selectedCity: "北京",
    agentSession: null,
    conversationTurns: [],
    itineraryPlan: null,
    planComparison: null,
    preferenceCard: null,
    preferenceMemory: null,
    pendingPoiCandidates: [],
    activeVersionId: null,
    selectedSegmentId: null,
    selectedRouteOptionId: null,
    previewRouteOptionId: null
  });
  const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) {
      return jsonResponse({ mode: "mock", default: [], mock: [] });
    }
    if (path.endsWith("/preferences/memory")) {
      return jsonResponse({
        userId: "test-user",
        memoryText: "",
        autoUpdateEnabled: true,
        createdAt: "2026-06-10T10:00:00Z",
        updatedAt: "2026-06-10T10:00:00Z"
      });
    }
    if (path.endsWith("/preferences/extract")) {
      return jsonResponse({ summaryCard: { ...preferenceCard(""), items: [], summaryText: "" } });
    }
    if (path.endsWith("/agent/sessions/current")) {
      return jsonResponse(agentSessionWithNightViewLocalOptions());
    }
    if (path.endsWith("/agent/sessions/sess_stream/messages") && init?.method === "POST") {
      return jsonResponse(agentMessageResponse());
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);

  await waitFor(() => expect(screen.getByText("手动输入 Day 2 街区漫步地点")).toBeTruthy());
  const input = screen.getByLabelText("自填选项内容");
  const sendButton = screen.getByRole("button", { name: "搜索候选" });
  expect((sendButton as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(sendButton);
  expect(fetchMock.mock.calls.some((call) => String(call[0]).endsWith("/agent/sessions/sess_stream/messages"))).toBe(
    false
  );
  fireEvent.change(input, { target: { value: "北京动物园" } });
  fireEvent.click(sendButton);

  await waitFor(() =>
    expect(fetchMock.mock.calls.some((call) => String(call[0]).endsWith("/agent/sessions/sess_stream/messages"))).toBe(
      true
    )
  );
  const messageCall = fetchMock.mock.calls.find((call) =>
    String(call[0]).endsWith("/agent/sessions/sess_stream/messages")
  );
  const messageBody = JSON.parse(String(messageCall?.[1]?.body));
  expect(messageBody.content).toBe("北京动物园");
  expect(messageBody.context.selectedAgentChoice).toEqual({
    sourceAssistantTurnId: "turn_night_options_assistant",
    choiceId: "manual_day_2_walk",
    manualValue: "北京动物园"
  });
});

function agentExecutionStreamResponse(finish: Promise<void>) {
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(
        encoder.encode(`${JSON.stringify({ event: "user_turn", data: agentMessageResponse().userTurn })}\n`)
      );
      controller.enqueue(
        encoder.encode(
          `${JSON.stringify({
            event: "execution_event",
            data: {
              type: "agent",
              label: "执行 Agent 工具循环",
              userVisible: false,
              status: "querying",
              detail: "Agent 正在选择并调用 read_itinerary、patch_itinerary、联网查询或偏好记忆等工具。",
              fallbackUsed: false,
              timestamp: "2026-06-10T10:00:00Z"
            }
          })}\n`
        )
      );
      controller.enqueue(
        encoder.encode(
          `${JSON.stringify({
            event: "execution_event",
            data: {
              type: "tool",
              label: "read_itinerary",
              actionLabel: "正在读取当前行程",
              userVisible: true,
              status: "querying",
              detail: "正在读取当前 active itinerary。",
              providerName: "sqlite-itinerary-read",
              fallbackUsed: false,
              durationMs: 1500,
              metadata: { runElapsedMs: 12340 },
              timestamp: "2026-06-10T10:00:01Z"
            }
          })}\n`
        )
      );
      controller.enqueue(
        encoder.encode(
          `${JSON.stringify({
            event: "execution_event",
            data: {
              type: "heartbeat",
              label: "Agent 执行中",
              status: "querying",
              detail: "后端仍在等待当前安全阶段完成。",
              providerName: "agent-stream",
              fallbackUsed: false,
              metadata: { phase: "stream_wait", runElapsedMs: 14340 },
              timestamp: "2026-06-10T10:00:03Z"
            }
          })}\n`
        )
      );
      controller.enqueue(
        encoder.encode(
          `${JSON.stringify({
            event: "reasoning_status",
            data: reasoningStatus(1, "running", "正在读取当前行程并检查可修改范围", "context")
          })}\n`
        )
      );
      finish.then(() => {
        controller.enqueue(
          encoder.encode(`${JSON.stringify({ event: "message_response", data: agentMessageResponse() })}\n`)
        );
        controller.close();
      });
    }
  });
  return new Response(stream, { status: 200, headers: { "Content-Type": "application/x-ndjson" } });
}

function comparisonStreamProjection(
  proposalId: string,
  title: string
): Record<string, unknown> & { choiceId: string; title: string } {
  return {
    planningSelectionRootTurnId: "root_stream",
    rootPortfolioId: "portfolio_stream",
    proposalId,
    sourceAssistantTurnId: "turn_stream_assistant",
    choiceId: `choice_${proposalId}`,
    status: "complete",
    isPartial: false,
    isAdopted: false,
    adoptionReady: true,
    activeVersionId: null,
    expectedBaseVersionId: null,
    title,
    days: [],
    pendingSlots: [],
    routeEvidence: [],
    budgetSummary: "medium budget",
    routeSummary: "verified route",
    tradeoffSummary: "",
    colorKey: stablePlanColorKey(proposalId)
  };
}

function comparisonProjectionFailureStreamResponse(
  response: AgentMessageResponse,
  comparisonProjection: Record<string, unknown>,
  message: string
) {
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(encoder.encode(`${JSON.stringify({ event: "user_turn", data: response.userTurn })}\n`));
      controller.enqueue(
        encoder.encode(
          `${JSON.stringify({
            event: "execution_event",
            data: {
              type: "portfolio_plan_visible",
              label: "later stream failed",
              status: "completed",
              detail: "later stream failure",
              userVisible: true,
              fallbackUsed: true,
              metadata: { comparisonProjection },
              timestamp: "2026-07-25T10:00:00Z"
            }
          })}\n`
        )
      );
      controller.enqueue(encoder.encode(`${JSON.stringify({ event: "error", data: { message, statusCode: 500 } })}\n`));
      controller.close();
    }
  });
  return new Response(stream, { status: 200, headers: { "Content-Type": "application/x-ndjson" } });
}
function comparisonProjectionStreamResponse(
  response: AgentMessageResponse,
  comparisonProjection: Record<string, unknown>
) {
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(encoder.encode(`${JSON.stringify({ event: "user_turn", data: response.userTurn })}\n`));
      controller.enqueue(
        encoder.encode(
          `${JSON.stringify({
            event: "execution_event",
            data: {
              type: "portfolio_plan_visible",
              label: "部分行程已可预览",
              status: "completed",
              detail: "已复用提交后的 active version 进入只读对比。",
              userVisible: true,
              fallbackUsed: true,
              metadata: { comparisonProjection },
              timestamp: "2026-07-25T10:00:00Z"
            }
          })}\n`
        )
      );
      controller.enqueue(encoder.encode(`${JSON.stringify({ event: "message_response", data: response })}\n`));
      controller.close();
    }
  });
  return new Response(stream, { status: 200, headers: { "Content-Type": "application/x-ndjson" } });
}

function agentCompletedExecutionStreamResponse(finish: Promise<void>) {
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(
        encoder.encode(`${JSON.stringify({ event: "user_turn", data: agentMessageResponse().userTurn })}\n`)
      );
      controller.enqueue(
        encoder.encode(
          `${JSON.stringify({
            event: "execution_event",
            data: {
              type: "tool",
              label: "read_itinerary",
              actionLabel: "正在读取当前行程",
              userVisible: true,
              status: "querying",
              detail: "正在读取当前 active itinerary。",
              providerName: "sqlite-itinerary-read",
              fallbackUsed: false,
              timestamp: "2026-06-10T10:00:01Z"
            }
          })}\n`
        )
      );
      controller.enqueue(
        encoder.encode(
          `${JSON.stringify({
            event: "reasoning_status",
            data: reasoningStatus(1, "running", "正在分析现有上下文", "context")
          })}\n`
        )
      );
      controller.enqueue(
        encoder.encode(
          `${JSON.stringify({
            event: "execution_event",
            data: {
              type: "tool",
              label: "read_itinerary",
              actionLabel: "正在读取当前行程",
              userVisible: true,
              status: "completed",
              detail: "已读取当前 active itinerary。",
              providerName: "sqlite-itinerary-read",
              fallbackUsed: false,
              timestamp: "2026-06-10T10:00:02Z"
            }
          })}\n`
        )
      );
      controller.enqueue(
        encoder.encode(
          `${JSON.stringify({
            event: "reasoning_status",
            data: reasoningStatus(2, "completed", "已完成上下文分析", "context")
          })}\n`
        )
      );
      finish.then(() => {
        controller.enqueue(
          encoder.encode(`${JSON.stringify({ event: "message_response", data: agentMessageResponse() })}\n`)
        );
        controller.close();
      });
    }
  });
  return new Response(stream, { status: 200, headers: { "Content-Type": "application/x-ndjson" } });
}

function reasoningStatus(
  sequence: number,
  status: AgentReasoningStatus["status"],
  summary: string,
  phase: AgentReasoningStatus["phase"]
): AgentReasoningStatus {
  return {
    messageType: "reasoning_status" as const,
    id: `reasoning_stream_${sequence}`,
    sequence,
    phase,
    status,
    summary,
    detail: null,
    sourceEventType: phase === "finalizing" ? "agent_run_terminal" : "resolve_poi",
    sessionId: "sess_stream",
    turnId: "turn_stream_user",
    timestamp: `2026-06-10T10:00:0${sequence}Z`
  };
}

function agentReasoningStreamResponse(response: AgentMessageResponse, finish: Promise<void>) {
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(encoder.encode(`${JSON.stringify({ event: "user_turn", data: response.userTurn })}\n`));
      controller.enqueue(
        encoder.encode(
          `${JSON.stringify({
            event: "reasoning_status",
            data: reasoningStatus(1, "running", "正在核验真实地图地点", "tool")
          })}\n`
        )
      );
      finish.then(() => {
        controller.enqueue(
          encoder.encode(
            `${JSON.stringify({
              event: "reasoning_status",
              data: reasoningStatus(2, "completed", "处理完成，正在展示结果", "finalizing")
            })}\n`
          )
        );
        controller.enqueue(encoder.encode(`${JSON.stringify({ event: "message_response", data: response })}\n`));
        controller.close();
      });
    }
  });
  return new Response(stream, { status: 200, headers: { "Content-Type": "application/x-ndjson" } });
}

function truncatedReasoningStreamResponse(response: AgentMessageResponse) {
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(encoder.encode(`${JSON.stringify({ event: "user_turn", data: response.userTurn })}\n`));
      controller.enqueue(
        encoder.encode(
          `${JSON.stringify({
            event: "reasoning_status",
            data: reasoningStatus(1, "running", "正在检查相关约束", "constraints")
          })}\n`
        )
      );
      controller.enqueue(
        encoder.encode(
          `${JSON.stringify({
            event: "reasoning_status",
            data: reasoningStatus(2, "completed", "已完成上下文分析", "context")
          })}\n`
        )
      );
      controller.close();
    }
  });
  return new Response(stream, { status: 200, headers: { "Content-Type": "application/x-ndjson" } });
}

function preUserTurnInterruptedStreamResponse() {
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.close();
    }
  });
  return new Response(stream, { status: 200, headers: { "Content-Type": "application/x-ndjson" } });
}

function agentExecutionFailureStreamResponse(finish: Promise<void>) {
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(
        encoder.encode(`${JSON.stringify({ event: "user_turn", data: agentMessageResponse().userTurn })}\n`)
      );
      controller.enqueue(
        encoder.encode(
          `${JSON.stringify({
            event: "execution_event",
            data: {
              type: "tool",
              label: "patch_itinerary",
              actionLabel: "正在校验时间轴写入",
              userVisible: true,
              status: "failed",
              detail: "Agent verifier rejected tool-loop itinerary write",
              providerName: "agent-verifier",
              fallbackUsed: false,
              failureReason: "Agent verifier rejected tool-loop itinerary write",
              timestamp: "2026-06-10T10:00:02Z"
            }
          })}\n`
        )
      );
      controller.enqueue(
        encoder.encode(
          `${JSON.stringify({
            event: "reasoning_status",
            data: reasoningStatus(1, "failed", "处理未完成", "verification")
          })}\n`
        )
      );
      finish.then(() => {
        controller.enqueue(
          encoder.encode(`${JSON.stringify({ event: "message_response", data: agentMessageResponse() })}\n`)
        );
        controller.close();
      });
    }
  });
  return new Response(stream, { status: 200, headers: { "Content-Type": "application/x-ndjson" } });
}

function agentExecutionErrorStreamResponse(message: string, statusCode: number, extra: Record<string, unknown> = {}) {
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(
        encoder.encode(
          `${JSON.stringify({
            event: "error",
            data: { message, statusCode, ...extra }
          })}\n`
        )
      );
      controller.close();
    }
  });
  return new Response(stream, { status: 200, headers: { "Content-Type": "application/x-ndjson" } });
}
function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" }
  });
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((innerResolve, innerReject) => {
    resolve = innerResolve;
    reject = innerReject;
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

function preferenceCard(summaryText: string) {
  return {
    id: "card_stream",
    profileId: "pref_stream",
    partySize: 2,
    travelerTypes: ["adult"],
    budgetRange: "3000 左右",
    pacePreference: "轻松不赶路",
    summaryText,
    items: [{ label: "公共交通优先", sourceText: "公共交通优先" }],
    status: "draft"
  };
}

function agentSession(): AgentSession {
  return {
    sessionId: "sess_stream",
    status: "active",
    city: "北京",
    title: "北京 AI 行程",
    activePlanId: "plan_stream",
    activeVersionId: null,
    turns: [],
    itinerary: null,
    pendingPoiCandidates: []
  };
}

function agentSessionWithClarificationOptions(): AgentSession {
  const createdAt = "2026-06-10T10:00:00Z";
  return {
    ...agentSession(),
    turns: [
      {
        id: "turn_option_user",
        role: "user",
        content: "想出去玩",
        turnIndex: 1,
        status: "active",
        createdAt,
        updatedAt: createdAt
      },
      {
        id: "turn_option_assistant",
        role: "assistant",
        content: "我先确认一个方向，再继续生成可编辑时间轴：这次大概玩几天？",
        choiceOptions: [
          { index: 1, label: "1 天精简版：先排核心 POI，适合快速验证路线。", value: "1 天", kind: "preset" },
          { index: 2, label: "2 天标准版：景点、交通和票务信息更完整。", value: "2 天", kind: "preset" },
          { index: 3, label: "3 天轻松版：降低每日密度，留更多机动时间。", value: "3 天", kind: "preset" },
          { index: 4, label: "我自己填写具体天数。", kind: "custom_input" }
        ],
        turnIndex: 2,
        status: "active",
        planningSteps: [],
        toolEvents: [],
        createdAt,
        updatedAt: createdAt
      }
    ]
  };
}

function agentSessionWithSafeFallbackOptions() {
  const createdAt = "2026-06-10T10:00:00Z";
  return {
    ...agentSession(),
    turns: [
      {
        id: "turn_fallback_user",
        role: "user",
        content: "北京两日游",
        turnIndex: 1,
        status: "active",
        createdAt,
        updatedAt: createdAt
      },
      {
        id: "turn_fallback_assistant",
        role: "assistant",
        content: "模型规划暂不可用，下一步怎么处理？",
        choiceOptions: [
          {
            id: "fallback:retry_model_planning:decision_1",
            index: 1,
            kind: "safe_fallback_action",
            action: "retry_model_planning",
            label: "重试模型规划",
            value: "重试模型规划",
            lifecycle: "offered",
            attempt: 1
          },
          {
            id: "fallback:confirm_rule_safe_draft:decision_1",
            index: 2,
            kind: "safe_fallback_action",
            action: "confirm_rule_safe_draft",
            label: "使用规则安全草稿",
            value: "使用规则安全草稿",
            lifecycle: "offered",
            attempt: 1
          },
          {
            id: "fallback:manual_continuation:decision_1",
            index: 3,
            kind: "custom_input",
            action: "manual_continuation",
            label: "我自己补充",
            value: "我自己补充",
            lifecycle: "offered",
            attempt: 1
          }
        ],
        turnIndex: 2,
        status: "active",
        planningSteps: [],
        toolEvents: [],
        createdAt,
        updatedAt: createdAt
      }
    ]
  };
}

function agentSessionWithStructuredPoiChoice() {
  const session = staleAgentSession();
  const createdAt = "2026-06-10T10:00:00Z";
  const amapPoi = {
    id: "amap_poi_2",
    name: "清华大学紫荆园",
    type: "餐饮服务",
    city: "北京",
    district: "海淀区",
    address: "清华大学校内",
    longitude: 116.326,
    latitude: 40.003,
    category: "food",
    source: "amap-place",
    sourceNote: "高德候选",
    confidence: 0.93,
    photos: []
  };
  return {
    ...session,
    turns: [
      {
        id: "turn_structured_assistant",
        role: "assistant",
        content: "请选择一个清华校内食堂候选，当前时间轴尚未修改。",
        choiceOptions: [
          {
            id: "choice_1",
            index: 1,
            label: "清华大学清芬园",
            value: "清华大学清芬园",
            kind: "poi_candidate",
            candidateRecordId: "candidate_record_1",
            amapId: "B000A1",
            segmentId: "seg_stale",
            amapPoi: { ...amapPoi, id: "amap_poi_1", name: "清华大学清芬园", longitude: 116.324, latitude: 40.001 },
            selectionGroupId: "selection_group_meal_1"
          },
          {
            id: "choice_2",
            index: 2,
            label: "清华大学紫荆园",
            value: "清华大学紫荆园",
            kind: "poi_candidate",
            candidateRecordId: "candidate_record_2",
            amapId: "B000A2",
            segmentId: "seg_stale",
            amapPoi,
            selectionGroupId: "selection_group_meal_1"
          },
          {
            id: "choice_3",
            index: 3,
            label: "清华大学桃李园",
            value: "清华大学桃李园",
            kind: "poi_candidate",
            candidateRecordId: "candidate_record_3",
            amapId: "B000A3",
            segmentId: "seg_stale",
            amapPoi: { ...amapPoi, id: "amap_poi_3", name: "清华大学桃李园", longitude: 116.328, latitude: 40.005 },
            selectionGroupId: "selection_group_meal_1"
          },
          {
            id: "choice_4",
            index: 4,
            label: "清华大学听涛园",
            value: "清华大学听涛园",
            kind: "poi_candidate",
            candidateRecordId: "candidate_record_4",
            amapId: "B000A4",
            segmentId: "seg_stale",
            amapPoi: { ...amapPoi, id: "amap_poi_4", name: "清华大学听涛园", longitude: 116.329, latitude: 40.006 },
            selectionGroupId: "selection_group_meal_1"
          },
          {
            id: "choice_other_group",
            index: 5,
            label: "中国美术馆",
            value: "中国美术馆",
            kind: "poi_candidate",
            candidateRecordId: "candidate_record_museum",
            amapId: "B000M1",
            segmentId: "seg_museum",
            amapPoi: { ...amapPoi, id: "amap_museum_1", name: "中国美术馆", category: "museum" },
            selectionGroupId: "selection_group_museum_1"
          },
          {
            id: "choice_manual",
            index: 6,
            label: "我自己填写具体地点",
            kind: "custom_input",
            selectionGroupId: "selection_group_meal_1"
          }
        ],
        turnIndex: 2,
        status: "active",
        planningSteps: [],
        toolEvents: [],
        createdAt,
        updatedAt: createdAt
      }
    ]
  };
}

function agentSessionWithNightViewLocalOptions() {
  const createdAt = "2026-06-10T10:00:00Z";
  return {
    ...agentSession(),
    turns: [
      {
        id: "turn_night_options_user",
        role: "user",
        content: "第二天的夜景观景点我先看北京的城市夜景",
        turnIndex: 1,
        status: "active",
        createdAt,
        updatedAt: createdAt
      },
      {
        id: "turn_night_options_assistant",
        role: "assistant",
        content: "第 2 天夜景我先给你几个真实高德地点候选，当前时间轴暂不改。",
        choiceOptions: [
          {
            id: "candidate_day_2_walk",
            index: 1,
            kind: "portfolio_density_candidate",
            action: "resume_density_candidate",
            label: "Day 2：天坛公园",
            lifecycle: "offered",
            briefId: "brief_beijing_two_day",
            poolId: "pool_day_2_walk",
            planningSlotId: "slot_day_2_walk",
            dayNumber: 2,
            intentType: "neighborhood_walk",
            expectedBaseVersionId: "ver_partial_beijing"
          },
          {
            id: "manual_day_2_walk",
            index: 2,
            kind: "custom_input",
            action: "manual_continuation",
            label: "手动输入 Day 2 街区漫步地点",
            lifecycle: "offered",
            briefId: "brief_beijing_two_day",
            poolId: "pool_day_2_walk",
            planningSlotId: "slot_day_2_walk",
            dayNumber: 2,
            intentType: "neighborhood_walk",
            expectedBaseVersionId: "ver_partial_beijing"
          }
        ],
        turnIndex: 2,
        status: "active",
        planningSteps: [],
        toolEvents: [],
        createdAt,
        updatedAt: createdAt
      }
    ]
  };
}

function staleAgentSession() {
  return {
    sessionId: "sess_stale",
    status: "active",
    city: "北京",
    title: "旧北京 AI 行程",
    activePlanId: "plan_stale",
    activeVersionId: "ver_stale",
    turns: [],
    itinerary: {
      id: "plan_stale",
      title: "旧 session 行程",
      city: "北京",
      templateType: "custom",
      budgetTarget: 3000,
      budgetEstimate: 0,
      budgetDeltaExplanation: "",
      decisionRationale: "",
      status: "draft",
      days: [],
      routeOptions: [],
      weatherSignals: [],
      trafficCrowdingSignals: [],
      ticketLookupResults: []
    },
    pendingPoiCandidates: []
  };
}

function agentMessageResponse(): AgentMessageResponse {
  const createdAt = "2026-06-10T10:00:00Z";
  return {
    userTurn: {
      id: "turn_stream_user",
      role: "user",
      content: "北京两天，轻松一点，想去故宫和胡同",
      turnIndex: 1,
      status: "active",
      createdAt,
      updatedAt: createdAt
    },
    assistantTurn: {
      id: "turn_stream_assistant",
      role: "assistant",
      content:
        "## 行程摘要\n\n已按规划过程生成可编辑行程。\n\n| 时段 | 安排 |\n| --- | --- |\n| 上午 | 故宫 |\n\n- **故宫**\n- [官方入口](https://example.com)",
      turnIndex: 2,
      status: "active",
      itineraryVersionId: "ver_stream",
      planningRunId: "run_stream",
      planningDirectionCount: 3,
      verifiedComparisonProposalCount: 1,
      planningSteps: planningEventsFixture(),
      toolEvents: planningEventsFixture(),
      createdAt,
      updatedAt: createdAt
    },
    itinerary: {
      id: "plan_stream",
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
    },
    version: { id: "ver_stream", versionNumber: 1, sourceType: "agent" },
    pendingPoiCandidates: [],
    preferenceMemory: {
      userId: "test-user",
      sessionId: "sess_stream",
      memoryText: "# 我的旅行偏好\n\n## 旅行节奏\n- 偏好轻松不赶路，避免单日安排过满。\n",
      structuredMemory: {
        version: "travel-memory-v1",
        facts: [
          {
            id: "mem_test_pace",
            scope: "trip",
            category: "pace",
            key: "pace.relaxed",
            value: "偏好轻松不赶路，避免单日安排过满。",
            status: "confirmed",
            source: "user_explicit",
            confidence: 0.9,
            evidence: "北京两天，轻松一点",
            updatedAt: createdAt
          }
        ],
        autoUpdateClassifications: []
      },
      compiledRules: {},
      pendingConfirmations: [],
      autoUpdateEnabled: true,
      createdAt,
      updatedAt: createdAt
    },
    warnings: [],
    planningRun: planningRunFixture(),
    planningSteps: planningEventsFixture(),
    toolEvents: planningEventsFixture()
  };
}

function partialTimelineAgentMessageResponse() {
  const response: any = agentMessageResponse();
  response.assistantTurn.content =
    "已生成可编辑草案：部分地点已完成地图候选选择，部分主题地点因绕路风险暂未写入，可稍后重试或手动补充。";
  response.assistantTurn.planningSteps = partialTimelinePlanningEventsFixture();
  response.assistantTurn.toolEvents = partialTimelinePlanningEventsFixture();
  response.assistantTurn.choiceOptions = [
    {
      id: "map_partial_slot",
      index: 1,
      label: "在地图对比 Day 1 候选位置",
      action: "open_density_map",
      kind: "portfolio_density_map",
      candidateRecordId: "candidate_partial_slot",
      briefId: "brief_partial",
      poolId: "pool_partial_1",
      planningSlotId: "slot_partial_1",
      dayNumber: 1,
      timeWindow: "14:00-16:00",
      displayNeed: "街区漫步",
      comparisonAnchors: []
    },
    {
      id: "refresh_partial_slot",
      index: 2,
      label: "刷新 Day 1 14:00-16:00 街区漫步候选",
      action: "refresh_density_candidates",
      kind: "portfolio_density_refresh",
      candidateRecordId: "candidate_partial_slot",
      briefId: "brief_partial",
      poolId: "pool_partial_1",
      planningSlotId: "slot_partial_1",
      dayNumber: 1
    }
  ];
  response.warnings = ["部分主题地点因绕路风险或候选质量不足未自动写入，可稍后重试或手动选择更顺路地点。"];
  response.itinerary = partialTimelineItineraryFixture();
  response.planningSteps = partialTimelinePlanningEventsFixture();
  response.toolEvents = partialTimelinePlanningEventsFixture();
  response.planningRun = {
    ...planningRunFixture(),
    itineraryPlanId: "plan_partial",
    itineraryVersionId: "ver_partial"
  };
  response.version = { id: "ver_partial", versionNumber: 2, sourceType: "agent" };
  return response;
}

function partialTimelineItineraryFixture() {
  return {
    id: "plan_partial",
    title: "北京峡谷漂流部分草案",
    city: "北京",
    templateType: "custom",
    budgetTarget: 800,
    budgetEstimate: 160,
    budgetDeltaExplanation: "部分地点待补全。",
    decisionRationale: "保留已完成地图候选选择的可编辑草案。",
    status: "draft",
    days: [
      {
        id: "day_partial_1",
        dayNumber: 1,
        title: "Day 1",
        weatherSummary: "旅行日期对应天气预报尚不可用",
        riskSummary: "待核对官方公告",
        totalEstimatedCost: 0,
        segments: [],
        pendingSlots: [
          {
            id: "pending_partial_1",
            planningSlotId: "slot_partial_1",
            briefId: "brief_partial",
            poolId: "pool_partial_1",
            dayNumber: 1,
            timeWindow: "14:00-16:00",
            startTime: "14:00",
            endTime: "16:00",
            durationMinutes: 120,
            rawNeed: "街区漫步",
            intentType: "neighborhood_walk",
            kind: "activity",
            state: "pending",
            label: "待补：街区漫步"
          }
        ]
      },
      {
        id: "day_partial_2",
        dayNumber: 2,
        title: "Day 2",
        weatherSummary: "旅行日期对应天气预报尚不可用",
        riskSummary: "待核对官方公告",
        totalEstimatedCost: 160,
        segments: [
          {
            id: "seg_partial_1",
            startTime: "09:00",
            endTime: "11:00",
            kind: "activity",
            poi: {
              id: "poi_partial_1",
              amapId: "amap_nearby_rafting",
              name: "近郊漂流景区",
              city: "北京",
              category: "scenic",
              latitude: 39.91,
              longitude: 116.31,
              source: "amap-place-search",
              sourceNote: "groundingStatus：agent_selected_candidate；routeAnchor=true",
              confidence: 0.92
            },
            transportMode: "transit",
            estimatedCost: 80,
            notes: "groundingStatus：agent_selected_candidate；routeAnchor=true"
          },
          {
            id: "seg_partial_2",
            startTime: "12:00",
            endTime: "13:00",
            kind: "meal",
            poi: {
              id: "poi_partial_meal",
              amapId: "amap_meal",
              name: "北京美食餐厅",
              city: "北京",
              category: "food",
              latitude: 39.92,
              longitude: 116.32,
              source: "amap-place-search",
              sourceNote: "requiredGrounding=true; groundingStatus：agent_selected_candidate；routeAnchor=true",
              confidence: 0.91
            },
            transportMode: "transit",
            estimatedCost: 80,
            notes: "requiredGrounding=true; groundingStatus：agent_selected_candidate；routeAnchor=true"
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

function partialTimelinePlanningEventsFixture() {
  return [
    {
      type: "collect_candidates",
      label: "检索高德候选",
      status: "completed",
      detail: "已按 IntentPool 检索真实高德候选，并基于 evidence score 完成集合选择。",
      providerName: "agent-staged-pipeline",
      fallbackUsed: false,
      metadata: {
        resultPreview: {
          unresolvedSlots: [
            {
              slotId: "day1_evening_night",
              dayNumber: 1,
              rawNeed: "夜景观景点",
              reason: "route_detour_penalty",
              nextAction: "choose_spatially_reasonable_candidate_or_retry"
            }
          ],
          poolReports: [
            {
              poolId: "night_view_pool",
              rawNeed: "夜景观景点",
              selectedCount: 1,
              coverageStatus: "partial",
              rejectedReasonCounts: { route_detour_penalty: 1 }
            }
          ]
        }
      },
      timestamp: "2026-06-10T10:00:00Z"
    },
    {
      type: "build_persistable_segment_plans",
      label: "构建可持久化日程段",
      status: "completed",
      detail: "已将 selectedCandidate 转成 PersistableSegmentPlan；未完成的宽泛 fallback 槽位保留为待补全元数据。",
      providerName: "agent-staged-pipeline",
      fallbackUsed: false,
      metadata: {
        resultPreview: {
          persistableSegmentCount: 2,
          finalization: {
            canCreateVersion: true,
            resultState: "partial_success",
            unresolvedPolicy: "persist_viable_partial_days"
          }
        }
      },
      timestamp: "2026-06-10T10:00:00Z"
    }
  ];
}

function planningEventsFixture() {
  return [
    {
      type: "context",
      label: "读取当前 itinerary / timeline context",
      status: "completed",
      detail: "已读取服务端 active itinerary 与 timeline 状态。",
      providerName: "sqlite",
      fallbackUsed: false,
      timestamp: "2026-06-10T10:00:00Z"
    },
    {
      type: "tool",
      label: "高德天气",
      status: "fallback",
      detail: "高德天气服务不可用，未覆盖当前行程。",
      providerName: "amap-weather",
      fallbackUsed: true,
      durationMs: 1250,
      failureReason: "missing amap key",
      metadata: {
        inputPreview: { city: "北京", date: "2026-06-10" },
        resultPreview: {
          city: "北京",
          weather: "多云",
          providerName: "amap-weather",
          failureReason: "missing amap key"
        }
      },
      timestamp: "2026-06-10T10:00:00Z"
    },
    {
      type: "tool",
      label: "联网搜索票务/预约",
      status: "failed",
      detail: "博查联网搜索失败，未使用 mock 数据。",
      providerName: "bocha-web-search",
      fallbackUsed: false,
      failureReason: "WEB_SEARCH_API_KEY is not configured.",
      metadata: {
        inputPreview: { query: "故宫博物院 开放时间 预约 门票 官方" },
        resultPreview: {
          query: "故宫博物院 开放时间 预约 门票 官方",
          validationFeedback: "budgetTarget: Input should be a valid number, unable to parse string as a number",
          results: [
            {
              title: "故宫博物院官网",
              url: "https://www.dpm.org.cn",
              snippet: "开放时间、预约与票务入口。"
            }
          ],
          providerName: "bocha-web-search",
          failureReason: "WEB_SEARCH_API_KEY is not configured."
        }
      },
      timestamp: "2026-06-10T10:00:00Z"
    },
    {
      type: "tool",
      label: "read_itinerary",
      status: "succeeded",
      detail: "已读取当前 active itinerary。",
      providerName: "sqlite-itinerary-read",
      fallbackUsed: false,
      metadata: {
        inputPreview: {},
        resultPreview: {
          activeVersionId: null,
          itinerary: null
        }
      },
      timestamp: "2026-06-10T10:00:00Z"
    },
    {
      type: "tool",
      label: "patch_itinerary",
      status: "succeeded",
      detail: "已通过 DeepSeek tool call 写入时间轴版本。",
      providerName: "sqlite-itinerary-patch",
      fallbackUsed: false,
      metadata: {
        inputPreview: { operations: [{ op: "replace_itinerary" }] },
        resultPreview: {
          activeVersionId: "ver_stream",
          providerName: "sqlite-itinerary-patch",
          fallbackUsed: false
        }
      },
      timestamp: "2026-06-10T10:00:00Z"
    },
    {
      type: "collect_candidates",
      label: "候选检索完成",
      status: "completed",
      detail: "已完成候选池诊断。",
      providerName: "agent-staged-pipeline",
      fallbackUsed: false,
      metadata: {
        resultPreview: {
          planningDirectionCount: 3,
          verifiedComparisonProposalCount: 1,
          untrustedPreview: { briefCount: 99, visibleProposalCount: 99 },
          poolReports: [
            {
              poolId: "campus_visit_pool",
              rawNeed: "高校参观",
              intentType: "campus_visit",
              targetCount: 4,
              candidateHintCount: 0,
              strictGenericPool: true,
              selectionThreshold: 0.86,
              queryCount: 3,
              candidateCount: 2,
              scoredCount: 2,
              rejectedCount: 1,
              selectedCount: 0,
              topCandidates: [
                {
                  name: "中央广播电视大学",
                  type: "科教文化服务",
                  city: "北京市",
                  score: 0.62,
                  components: { typeMatch: 0.2 },
                  decision: "rejected",
                  threshold: 0.86,
                  rejectedReasons: ["semantic_mismatch"]
                }
              ],
              rejectedReasonCounts: { semantic_mismatch: 1 }
            }
          ],
          unresolvedSlots: [
            {
              slotId: "day-1-morning",
              dayNumber: 1,
              timeWindow: "morning",
              rawNeed: "高校参观",
              kind: "visit",
              intentType: "campus_visit",
              poolId: "campus_visit_pool",
              reason: "candidate_score_below_threshold",
              candidateHintCount: 0,
              strictGenericPool: true,
              topCandidatePreview: "中央广播电视大学",
              nextAction: "ask_user_for_candidate_hint_or_retry_with_better_hints"
            }
          ]
        }
      },
      timestamp: "2026-06-10T10:00:00Z"
    },
    {
      type: "build_persistable_segment_plans",
      label: "构建可持久化 segments",
      status: "fallback",
      detail: "没有 Day 达到最小可用行程标准。",
      providerName: "agent-staged-pipeline",
      fallbackUsed: true,
      metadata: {
        persistableSegmentCount: 0,
        routeAnchorRequiredCount: 6,
        routeAnchorSelectedCount: 0,
        dayReadiness: [
          {
            dayNumber: 1,
            requiredRouteAnchors: 3,
            selectedRouteAnchors: 0,
            optionalSegments: 2,
            unresolvedRouteAnchors: 3,
            meetsMinimumViableDay: false,
            blockingReasons: ["not_enough_routeable_anchors"]
          }
        ],
        unresolvedDays: [1],
        minimumViableRule: "each persisted day requires at least 2 routeable routeAnchor segments"
      },
      timestamp: "2026-06-10T10:00:00Z"
    },
    {
      type: "collect_candidates",
      label: "候选检索完成（旧格式）",
      status: "completed",
      detail: "兼容旧的顶层诊断 metadata。",
      providerName: "agent-staged-pipeline",
      fallbackUsed: false,
      metadata: {
        poolReports: [
          {
            poolId: "night_view_pool",
            rawNeed: "夜景观景点",
            intentType: "night_view",
            targetCount: 2,
            candidateHintCount: 0,
            strictGenericPool: true,
            selectionThreshold: 0.86,
            candidateCount: 1,
            selectedCount: 0
          }
        ]
      },
      timestamp: "2026-06-10T10:00:00Z"
    },
    {
      type: "create_itinerary_version",
      label: "创建 itinerary version",
      status: "fallback",
      detail: "本轮未创建 itinerary version。",
      providerName: "agent-staged-pipeline",
      fallbackUsed: true,
      metadata: {
        versionCreated: false,
        reason: "no_minimum_viable_day",
        blockingStage: "build_persistable_segment_plans",
        planningPreview: {
          message: "规划预览已生成，但尚未创建正式时间轴。"
        }
      },
      timestamp: "2026-06-10T10:00:00Z"
    }
  ];
}

function planningRunFixture() {
  return {
    id: "run_stream",
    runType: "agent_message",
    userInput: "北京两天，轻松一点，想去故宫和胡同",
    preferenceSummary: "用户偏好轻松不赶路，预算约 3000 元，公共交通优先。",
    itineraryPlanId: "plan_stream",
    itineraryVersionId: "ver_stream",
    understoodRequirements: {
      summary: "城市：北京；天数：2 天",
      missingFields: [],
      clarificationQuestions: []
    },
    constraintSummary: [{ label: "城市", value: "北京" }],
    toolCalls: [
      {
        id: "amap-weather",
        toolName: "高德天气",
        status: "fallback",
        providerName: "mock-amap-weather-provider",
        sourceName: "高德天气",
        queriedAt: "2026-06-10T10:00:00Z",
        confidence: 0.5,
        fallbackUsed: true,
        failureReason: "missing amap key",
        userVisibleCaveat: "高德天气 provider 不可用，已使用 fallback。",
        summary: "多云，16-24°C"
      },
      {
        id: "web-search-ticket",
        toolName: "联网搜索票务/预约",
        status: "failed",
        providerName: "bocha-web-search",
        sourceName: "Bocha 搜索失败",
        queriedAt: "2026-06-10T10:00:00Z",
        confidence: 0,
        fallbackUsed: false,
        failureReason: "WEB_SEARCH_API_KEY is not configured.",
        userVisibleCaveat: "博查联网搜索失败，未使用 mock 数据。",
        summary: "票务查询失败"
      }
    ],
    sourceAssessments: [
      {
        sourceName: "Bocha 搜索失败",
        sourceUrl: null,
        credibilityRank: "unavailable",
        credibilityLabel: "查询失败",
        providerName: "bocha-web-search",
        confidence: 0,
        fallbackUsed: false,
        conflictDetected: false,
        conflictReason: "",
        recommendation: "联网查询失败，未返回真实来源；请检查 provider 配置或人工核对官方渠道。"
      }
    ],
    feasibilityReport: {
      score: 78,
      riskLevel: "medium",
      issues: [
        {
          code: "day_density_high",
          severity: "medium",
          dimension: "schedule_density",
          message: "Day 1 安排了 4 个 POI/活动，可能偏赶。",
          recommendation: "建议把最后一个活动移到新增日期。",
          affectedDayId: "day_1",
          affectedSegmentId: "seg_4",
          evidence: ["actual=4"]
        }
      ],
      suggestions: ["建议把最后一个活动移到新增日期。"],
      localReplanSuggestions: [
        {
          id: "sug_stream",
          issueCode: "day_density_high",
          actionType: "reduce_day_density",
          summary: "把当天最后一个活动移到新的一天，降低当天密度。",
          rationale: "只影响一个问题，不全量重生成。",
          requiresConfirmation: true,
          operations: [
            { op: "add_day", title: "新增轻松安排" },
            { op: "move_segment", segmentId: "seg_4", targetDayId: "__new_day__", startTime: "09:30" }
          ]
        }
      ],
      preferenceAlignment: "偏好摘要已用于降低单日密度和识别赶路风险。",
      checkedAt: "2026-06-10T10:00:00Z"
    },
    finalSummary: "已按规划过程生成可编辑行程。",
    createdAt: "2026-06-10T10:00:00Z"
  };
}

function localReplanPatchResponse() {
  return {
    itinerary: {
      ...itineraryAfterLocalReplan(),
      feasibilityReport: planningRunApplyFixture().feasibilityReport,
      localReplanSuggestions: []
    },
    patch: { id: "patch_local_replan", validationStatus: "accepted" },
    version: { id: "ver_local_replan", versionNumber: 2, sourceType: "local_replan" },
    validationErrors: [],
    planningRun: planningRunApplyFixture()
  };
}

function itineraryAfterLocalReplan() {
  return {
    id: "plan_stream",
    title: "北京地图行程草案",
    city: "北京",
    templateType: "custom",
    budgetTarget: 3000,
    budgetEstimate: 120,
    budgetDeltaExplanation: "已参考偏好预算。",
    decisionRationale: "已应用偏好：轻松不赶路。",
    status: "draft",
    days: [
      {
        id: "day_1",
        dayNumber: 1,
        title: "Day 1",
        weatherSummary: "多云",
        riskSummary: "中风险",
        totalEstimatedCost: 80,
        segments: []
      },
      {
        id: "day_2",
        dayNumber: 2,
        title: "新增轻松安排",
        weatherSummary: "多云",
        riskSummary: "低风险",
        totalEstimatedCost: 40,
        segments: [
          {
            id: "seg_4",
            startTime: "09:30",
            endTime: "10:30",
            kind: "activity",
            poi: {
              id: "poi_4",
              name: "景山公园",
              city: "北京",
              category: "scenic",
              latitude: 39.9236,
              longitude: 116.3969,
              source: "amap-place-search",
              confidence: 0.9
            },
            transportMode: "walk",
            estimatedCost: 40,
            notes: "移到新增日期，降低 Day 1 密度。"
          }
        ]
      }
    ],
    routeOptions: [],
    weatherSignals: [],
    trafficCrowdingSignals: [],
    ticketLookupResults: [],
    routeWarnings: []
  };
}

function planningRunApplyFixture() {
  return {
    id: "run_local_replan",
    runType: "local_replan_apply",
    userInput: "用户确认应用局部优化建议。",
    preferenceSummary: "用户偏好轻松不赶路，预算约 3000 元，公共交通优先。",
    itineraryPlanId: "plan_stream",
    itineraryVersionId: "ver_local_replan",
    understoodRequirements: {
      summary: "已应用局部优化",
      missingFields: [],
      clarificationQuestions: []
    },
    constraintSummary: [{ label: "城市", value: "北京" }],
    toolCalls: [
      {
        id: "agent-understanding",
        toolName: "Agent 目标理解",
        status: "completed",
        providerName: "agent-service",
        sourceName: "",
        queriedAt: "2026-06-10T10:05:00Z",
        confidence: 0.86,
        fallbackUsed: false,
        userVisibleCaveat: "",
        summary: "已按用户确认应用局部优化。"
      }
    ],
    sourceAssessments: [],
    feasibilityReport: {
      score: 88,
      riskLevel: "low",
      issues: [],
      suggestions: [],
      localReplanSuggestions: [],
      preferenceAlignment: "已减少单日密度，更符合轻松不赶路偏好。",
      checkedAt: "2026-06-10T10:05:00Z"
    },
    finalSummary: "已按用户确认应用局部优化，并重新检查行程可行性。",
    createdAt: "2026-06-10T10:05:00Z"
  };
}

test("expired portfolio reports regeneration action without entering POI selection mode", async () => {
  plannerStore.setState({
    selectedCity: "北京",
    agentSession: null,
    conversationTurns: [],
    itineraryPlan: null,
    planComparison: null,
    preferenceCard: null,
    preferenceMemory: null,
    pendingPoiCandidates: [],
    activeVersionId: null,
    selectedSegmentId: null,
    selectedRouteOptionId: null,
    previewRouteOptionId: null
  });
  const session = agentSessionWithStructuredPoiChoice();
  const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) return jsonResponse({ mode: "mock", default: [], mock: [] });
    if (path.endsWith("/preferences/memory"))
      return jsonResponse({ userId: "test-user", memoryText: "", autoUpdateEnabled: true });
    if (path.endsWith("/preferences/extract"))
      return jsonResponse({ summaryCard: { ...preferenceCard(""), items: [], summaryText: "" } });
    if (path.endsWith("/agent/sessions/current")) return jsonResponse(session);
    if (path.endsWith("/agent/sessions/sess_stale/messages/stream") && init?.method === "POST") {
      return agentExecutionErrorStreamResponse(
        "该方案已超过可选择时限，请重新生成方案后再选择。未创建正式行程。",
        409,
        {
          code: "plan_proposal_expired",
          details: { recommendedNextActions: ["regenerate_portfolio"] }
        }
      );
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);
  expect(await screen.findByRole("button", { name: /清华大学紫荆园/ })).toBeTruthy();
  const exactSessionCallsBeforeClick = fetchMock.mock.calls.filter((call) =>
    String(call[0]).endsWith("/agent/sessions/sess_stale")
  ).length;

  fireEvent.click(screen.getByRole("button", { name: /清华大学紫荆园/ }));

  expect(await screen.findByText("该方案已超过可选择时限，请重新生成方案后再选择。未创建正式行程。")).toBeTruthy();
  expect(screen.getByRole("button", { name: /清华大学紫荆园/ })).toBeTruthy();
  expect(fetchMock.mock.calls.filter((call) => String(call[0]).endsWith("/agent/sessions/sess_stale"))).toHaveLength(
    exactSessionCallsBeforeClick
  );
});

test("comparison card click and detail gestures preserve comparison and read-only contracts", async () => {
  const projection = comparisonStreamProjection("proposal_interaction", "交互测试方案");
  const session = {
    ...agentSession(),
    turns: [
      {
        id: "assistant_interaction",
        role: "assistant",
        content: "已生成可对比方案。",
        turnIndex: 1,
        status: "active",
        comparisonProjectionUpdateMode: "append",
        comparisonProjections: [{ ...projection, sourceAssistantTurnId: "assistant_interaction" }],
        choiceOptions: [
          {
            id: projection.choiceId,
            kind: "plan_proposal",
            action: "select_plan_proposal",
            label: projection.title,
            comparisonProjection: {
              ...projection,
              sourceAssistantTurnId: "assistant_interaction"
            }
          }
        ],
        planningSteps: [],
        toolEvents: [],
        createdAt: "2026-08-07T01:00:00Z",
        updatedAt: "2026-08-07T01:00:00Z"
      }
    ]
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: RequestInfo | URL) => {
      const path = String(url);
      if (path.endsWith("/providers/status")) return jsonResponse({ mode: "mock", default: [], mock: [] });
      if (path.endsWith("/preferences/extract")) return jsonResponse({ summaryCard: preferenceCard("") });
      if (path.endsWith("/agent/sessions/current")) return jsonResponse(session);
      return jsonResponse({}, 404);
    })
  );

  render(<AppShell />);
  await screen.findByText("已生成可对比方案。");
  fireEvent.click(screen.getByRole("tab", { name: "行程对比" }));
  const card = await screen.findByLabelText("方案 1：交互测试方案");

  fireEvent.click(card);
  expect(document.getElementById("itinerary-comparison-panel")).not.toBeNull();
  expect(plannerStore.getSnapshot().comparisonPreview.isMapReadOnly).toBe(true);

  fireEvent.click(within(card).getByRole("button", { name: "查看详情" }));
  expect(document.getElementById("itinerary-overview-panel")).not.toBeNull();
  expect(plannerStore.getSnapshot().comparisonPreview.isMapReadOnly).toBe(true);

  fireEvent.click(screen.getByRole("tab", { name: "行程对比" }));
  const focusedCard = await screen.findByLabelText("方案 1：交互测试方案");
  fireEvent.doubleClick(focusedCard);
  expect(document.getElementById("itinerary-overview-panel")).not.toBeNull();
  expect(plannerStore.getSnapshot().comparisonPreview.isMapReadOnly).toBe(true);
});

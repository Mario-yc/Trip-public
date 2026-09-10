import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { AppShell } from "../../src/components/AppShell";
import type {
  AgentMessageResponse,
  AgentSession,
  ConversationTurn,
  ItineraryPlan
} from "../../src/services/apiClient";
import {
  comparisonPreviewFromTurns,
  createComparisonPreviewState,
  markComparisonPlanAdopted,
  upsertVisibleComparisonPlan,
  type ComparisonPlanProjection
} from "../../src/state/planComparisonPreview";
import { plannerStore } from "../../src/state/plannerStore";

const CREATED_AT = "2026-08-18T01:00:00Z";
const SESSION_ID = "sess_simple_direction";
const ROOT_ID = "root_simple_direction";
const PORTFOLIO_ID = "portfolio_simple_direction";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  window.localStorage.removeItem("trip.activeAgentSessionId");
  resetPlannerStore();
});

test("the first travel request from an empty overview reports the real view and reaches the server", async () => {
  let session = agentSession([], null, null);
  seedPlanner(session);
  const requestBodies: Array<Record<string, unknown>> = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = new URL(String(input)).pathname;
    const method = init?.method ?? "GET";
    if (path.endsWith("/providers/status")) return jsonResponse(providerStatus());
    if (path.endsWith("/map/config")) return jsonResponse({ provider: "amap", enabled: false, jsApiKey: "" });
    if (path.endsWith("/agent/sessions") && method === "GET") {
      return jsonResponse({ sessions: [sessionSummary(session)] });
    }
    if (path.endsWith("/agent/sessions/current")) return jsonResponse(session);
    if (path.includes("/versions/saved")) return jsonResponse({ savedVersions: [] });
    if (path.endsWith(`/agent/sessions/${SESSION_ID}/messages/stream`) && method === "POST") {
      return jsonResponse({ detail: "stream disabled" }, 404);
    }
    if (path.endsWith(`/agent/sessions/${SESSION_ID}/messages`) && method === "POST") {
      const body = JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>;
      requestBodies.push(body);
      const response: AgentMessageResponse = {
        userTurn: turn("user_first_direction", "user", 1, "北京高校两日游，公共交通优先"),
        assistantTurn: turn("assistant_first_direction", "assistant", 2, "正在生成第一个方向。"),
        itinerary: null,
        version: null,
        pendingPoiCandidates: [],
        warnings: []
      };
      session = agentSession([response.userTurn, response.assistantTurn], null, null);
      return jsonResponse(response);
    }
    return jsonResponse({}, 404);
  }));

  render(<AppShell />);
  const input = await screen.findByLabelText("Agent 对话文本");
  fireEvent.change(input, { target: { value: "北京高校两日游，公共交通优先" } });
  fireEvent.keyDown(input, { key: "Enter" });

  await waitFor(() => expect(requestBodies).toHaveLength(1));
  expect(agentViewContext(requestBodies[0])).toEqual({
    schemaVersion: "agent-view-context-v1",
    activeView: "overview",
    editingProposal: null,
    focusedProposal: null
  });
  expect(screen.queryByText(/缺少当前编辑方案的服务端身份/)).toBeNull();
});

test("proposal cards, day blocks, and map legend switch the map to the intended proposal day", async () => {
  const planA = twoDayItinerary("a", "高校集中方向");
  const planB = twoDayItinerary("b", "公园夜游方向");
  const initialA = direction(planA, "direction_a", "assistant_initial", "confirm_a_0");
  const initialB = direction(planB, "direction_b", "assistant_initial", "confirm_b_0");
  const session = agentSession([carrier("assistant_initial", 1, [initialA, initialB], "replace")], null, null);
  seedPlanner(session);
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = new URL(String(input)).pathname;
    const method = init?.method ?? "GET";
    if (path.endsWith("/providers/status")) return jsonResponse(providerStatus());
    if (path.endsWith("/map/config")) return jsonResponse({ provider: "amap", enabled: false, jsApiKey: "" });
    if (path.endsWith("/agent/sessions") && method === "GET") {
      return jsonResponse({ sessions: [sessionSummary(session)] });
    }
    if (path.endsWith("/agent/sessions/current")) return jsonResponse(session);
    if (path.includes("/versions/saved")) return jsonResponse({ savedVersions: [] });
    return jsonResponse({}, 404);
  }));

  render(<AppShell />);
  fireEvent.click(await screen.findByRole("tab", { name: "行程对比" }));
  await waitFor(() => expect(document.querySelector('[data-proposal-id="direction_b"]')).not.toBeNull());

  plannerStore.setState({ selectedDayNumber: 2 });
  fireEvent.click(card("direction_b"));
  await waitFor(() => {
    expect(plannerStore.getSnapshot().comparisonPreview.focusedProposalId).toBe("direction_b");
    expect(plannerStore.getSnapshot().selectedDayNumber).toBe(1);
  });
  expect(await screen.findByRole("button", { name: "选择 公园夜游方向站点" })).toBeTruthy();
  expect(screen.queryByRole("button", { name: "选择 公园夜游方向第二天站点" })).toBeNull();
  expect(screen.queryByRole("button", { name: "选择 高校集中方向站点" })).toBeNull();

  fireEvent.click(
    within(card("direction_b")).getByRole("button", { name: "查看方案 2 Day 2 地图" })
  );
  await waitFor(() => expect(plannerStore.getSnapshot().selectedDayNumber).toBe(2));
  expect(await screen.findByRole("button", { name: "选择 公园夜游方向第二天站点" })).toBeTruthy();
  expect(screen.queryByRole("button", { name: "选择 公园夜游方向站点" })).toBeNull();

  const legend = screen.getByLabelText("方案地图图例");
  fireEvent.click(within(legend).getByRole("button", { name: "方案 1：高校集中方向" }));
  await waitFor(() => {
    expect(plannerStore.getSnapshot().comparisonPreview.focusedProposalId).toBe("direction_a");
    expect(plannerStore.getSnapshot().selectedDayNumber).toBe(1);
  });
  expect(await screen.findByRole("button", { name: "选择 高校集中方向站点" })).toBeTruthy();
  expect(screen.queryByRole("button", { name: "选择 高校集中方向第二天站点" })).toBeNull();
  expect(screen.queryByRole("button", { name: "选择 公园夜游方向第二天站点" })).toBeNull();
});

test("a late confirmation response cannot adopt a direction superseded by a new planning root", async () => {
  const planA = itinerary("late_a", "高校集中方向");
  const planB = itinerary("new_b", "新一轮公园方向");
  const directionA = direction(planA, "direction_a", "assistant_initial", "confirm_a_0");
  const initialCarrier = carrier("assistant_initial", 1, [directionA], "replace");
  let session = agentSession([initialCarrier], null, null);
  seedPlanner(session);
  const pendingConfirmation = deferred<Response>();
  let confirmationRequestCount = 0;
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = new URL(String(input)).pathname;
    const method = init?.method ?? "GET";
    if (path.endsWith("/providers/status")) return jsonResponse(providerStatus());
    if (path.endsWith("/map/config")) return jsonResponse({ provider: "amap", enabled: false, jsApiKey: "" });
    if (path.endsWith("/agent/sessions") && method === "GET") {
      return jsonResponse({ sessions: [sessionSummary(session)] });
    }
    if (path.endsWith("/agent/sessions/current")) return jsonResponse(session);
    if (path.includes("/versions/saved")) return jsonResponse({ savedVersions: [] });
    if (path.endsWith(`/agent/sessions/${SESSION_ID}/messages/stream`) && method === "POST") {
      return jsonResponse({ detail: "stream disabled in contract test" }, 404);
    }
    if (path.endsWith(`/agent/sessions/${SESSION_ID}/messages`) && method === "POST") {
      confirmationRequestCount += 1;
      return pendingConfirmation.promise;
    }
    return jsonResponse({}, 404);
  }));

  render(<AppShell />);
  fireEvent.click(await screen.findByRole("tab", { name: "行程对比" }));
  fireEvent.click(
    await screen.findByRole("button", { name: "确认编辑「高校集中方向」" })
  );
  await waitFor(() => expect(confirmationRequestCount).toBe(1));

  const directionB = {
    ...direction(planB, "direction_b", "assistant_new_root", "confirm_b_0"),
    planningSelectionRootTurnId: "root_new",
    rootPortfolioId: "portfolio_new"
  };
  const newRootCarrier = carrier("assistant_new_root", 2, [directionB], "replace");
  const current = plannerStore.getSnapshot();
  const supersedingSession = agentSession([initialCarrier, newRootCarrier], null, null);
  plannerStore.setState({
    agentSession: supersedingSession,
    conversationTurns: supersedingSession.turns,
    comparisonPreview: upsertVisibleComparisonPlan(current.comparisonPreview, directionB).state
  });

  const lateResponse = confirmationResponse(
    1,
    directionA.sourceAssistantTurnId,
    directionA.choiceId,
    planA,
    "ver_a_0"
  );
  session = agentSession(
    [...supersedingSession.turns, lateResponse.userTurn, lateResponse.assistantTurn],
    planA,
    "ver_a_0"
  );
  pendingConfirmation.resolve(jsonResponse(lateResponse));

  await waitFor(() => expect(plannerStore.getSnapshot().activeVersionId).toBe("ver_a_0"));
  const preview = plannerStore.getSnapshot().comparisonPreview;
  expect(preview.planningSelectionRootTurnId).toBe("root_new");
  expect(preview.rootPortfolioId).toBe("portfolio_new");
  expect(preview.adoptedProposalId).toBeNull();
  expect(preview.adoptedVersionId).toBeNull();
  expect(preview.mapMode).toBe("plan_comparison_preview");
  expect(preview.isMapReadOnly).toBe(true);
  expect(screen.getByRole("tab", { name: "行程对比" }).getAttribute("aria-selected")).toBe("true");
});

test("confirms A, saves it in place, confirms B, saves it in place, then confirms A again", async () => {
  const planA0 = itinerary("a", "高校集中方向");
  const planA1 = itinerary("a_edited", "高校集中方向（已编辑）");
  const planA2 = itinerary("a_reopened", "高校集中方向（再次编辑）");
  const planB0 = itinerary("b", "公园夜游方向");
  const planB1 = itinerary("b_edited", "公园夜游方向（已编辑）");
  const initialA = direction(planA0, "direction_a", "assistant_initial", "confirm_a_0");
  const initialB = direction(planB0, "direction_b", "assistant_initial", "confirm_b_0");
  const initialCarrier = carrier("assistant_initial", 1, [initialA, initialB], "replace");
  let session = agentSession([initialCarrier], null, null);
  seedPlanner(session);

  const messageBodies: Array<Record<string, unknown>> = [];
  const saveBodies: Array<{ proposalId: string; body: Record<string, unknown> }> = [];
  let confirmationCount = 0;
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const path = new URL(url).pathname;
    const method = init?.method ?? "GET";
    if (path.endsWith("/providers/status")) return jsonResponse(providerStatus());
    if (path.endsWith("/map/config")) return jsonResponse({ provider: "amap", enabled: false, jsApiKey: "" });
    if (path.endsWith("/agent/sessions") && method === "GET") {
      return jsonResponse({ sessions: [sessionSummary(session)] });
    }
    if (path.endsWith("/agent/sessions/current")) return jsonResponse(session);
    if (path.includes("/versions/saved")) return jsonResponse({ savedVersions: [] });
    if (path.endsWith(`/agent/sessions/${SESSION_ID}/messages/stream`) && method === "POST") {
      return jsonResponse({ detail: "stream disabled in contract test" }, 404);
    }
    if (path.endsWith(`/agent/sessions/${SESSION_ID}/messages`) && method === "POST") {
      const body = JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>;
      messageBodies.push(body);
      const selected = selectedChoice(body);
      confirmationCount += 1;
      const selectedA = selected.choiceId.startsWith("confirm_a");
      const nextPlan = selectedA
        ? confirmationCount === 3 ? planA2 : planA0
        : planB0;
      const nextVersion = selectedA
        ? confirmationCount === 3 ? "ver_a_2" : "ver_a_0"
        : "ver_b_0";
      const response = confirmationResponse(
        confirmationCount,
        selected.sourceAssistantTurnId,
        selected.choiceId,
        nextPlan,
        nextVersion
      );
      session = agentSession(
        [...session.turns, response.userTurn, response.assistantTurn],
        nextPlan,
        nextVersion
      );
      return jsonResponse(response);
    }
    const saveMatch = path.match(/\/agent\/sessions\/[^/]+\/directions\/([^/]+)\/save-active$/);
    if (saveMatch && method === "POST") {
      const proposalId = decodeURIComponent(saveMatch[1]);
      const body = JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>;
      saveBodies.push({ proposalId, body });
      const savingA = proposalId === "direction_a";
      const savedPlan = savingA ? planA1 : planB1;
      const sourceAssistantTurnId = savingA ? "assistant_saved_a" : "assistant_saved_b";
      const savedA = direction(
        savingA ? savedPlan : planA1,
        "direction_a",
        sourceAssistantTurnId,
        savingA ? "confirm_a_1" : "confirm_a_2",
        savingA,
        String(body.baseVersionId)
      );
      const savedB = direction(
        savingA ? planB0 : savedPlan,
        "direction_b",
        sourceAssistantTurnId,
        savingA ? "confirm_b_1" : "confirm_b_2",
        !savingA,
        String(body.baseVersionId)
      );
      const savedCarrier = carrier(
        sourceAssistantTurnId,
        session.turns.length + 1,
        [savedA, savedB],
        "replace",
        "internal_capability"
      );
      session = agentSession(
        [...session.turns, savedCarrier],
        savedPlan,
        String(body.baseVersionId)
      );
      return jsonResponse({
        proposalId,
        activeVersionId: body.baseVersionId,
        comparisonProjection: savingA ? savedA : savedB,
        saved: true,
        unchanged: false
      });
    }
    if (path.endsWith(`/agent/sessions/${SESSION_ID}`) && method === "GET") {
      return jsonResponse(session);
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);
  fireEvent.click(await screen.findByRole("tab", { name: "行程对比" }));
  await waitFor(() => expect(document.querySelector('[data-proposal-id="direction_a"]')).not.toBeNull());
  const initialAButton = within(card("direction_a")).getByRole("button", {
    name: "确认编辑「高校集中方向」"
  });
  const initialBButton = within(card("direction_b")).getByRole("button", {
    name: "确认编辑「公园夜游方向」"
  });
  expect(initialAButton.textContent).not.toBe(initialBButton.textContent);
  fireEvent.click(initialAButton);
  await waitFor(() => expect(plannerStore.getSnapshot().activeVersionId).toBe("ver_a_0"));
  expect(screen.getByRole("tab", { name: "行程总览" }).getAttribute("aria-selected")).toBe("true");

  replaceActiveItinerary(planA1, "ver_a_1");
  fireEvent.click(screen.getByRole("tab", { name: "行程对比" }));
  await waitFor(() => expect(screen.getByRole("tab", { name: "行程对比" }).getAttribute("aria-selected")).toBe("true"));
  expect(within(card("direction_a")).getByRole("heading").textContent).toContain("已编辑");
  expect(document.querySelector(".chat-row.internal_capability")).toBeNull();
  expect(saveBodies[0]).toEqual({
    proposalId: "direction_a",
    body: {
      baseVersionId: "ver_a_1",
      planningSelectionRootTurnId: ROOT_ID,
      rootPortfolioId: PORTFOLIO_ID
    }
  });

  fireEvent.click(
    within(card("direction_b")).getByRole("button", { name: "确认编辑「公园夜游方向」" })
  );
  await waitFor(() => expect(plannerStore.getSnapshot().activeVersionId).toBe("ver_b_0"));
  replaceActiveItinerary(planB1, "ver_b_1");
  fireEvent.click(screen.getByRole("tab", { name: "行程对比" }));
  await waitFor(() => expect(screen.getByRole("tab", { name: "行程对比" }).getAttribute("aria-selected")).toBe("true"));
  expect(within(card("direction_b")).getByRole("heading").textContent).toContain("已编辑");

  fireEvent.click(
    within(card("direction_a")).getByRole("button", { name: "确认编辑「高校集中方向（已编辑）」" })
  );
  await waitFor(() => expect(plannerStore.getSnapshot().activeVersionId).toBe("ver_a_2"));
  expect(plannerStore.getSnapshot().comparisonPreview.adoptedProposalId).toBe("direction_a");
  expect(screen.getByRole("tab", { name: "行程总览" }).getAttribute("aria-selected")).toBe("true");
  expect(messageBodies.map((body) => selectedChoice(body))).toEqual([
    { sourceAssistantTurnId: "assistant_initial", choiceId: "confirm_a_0" },
    { sourceAssistantTurnId: "assistant_saved_a", choiceId: "confirm_b_1" },
    { sourceAssistantTurnId: "assistant_saved_b", choiceId: "confirm_a_2" }
  ]);
  expect(agentViewContext(messageBodies[1])).toMatchObject({
    activeView: "comparison",
    editingProposal: { proposalId: "direction_a", activeVersionId: "ver_a_1" }
  });
  expect(saveBodies.map((item) => item.proposalId)).toEqual(["direction_a", "direction_b"]);
});

test("a failed save blocks click and keyboard switches and coalesces the duplicate write", async () => {
  const planA = itinerary("a", "高校集中方向");
  const projection = direction(
    planA,
    "direction_a",
    "assistant_initial",
    "confirm_a_0",
    true,
    "ver_a_1"
  );
  const session = agentSession([carrier("assistant_initial", 1, [projection], "replace")], planA, "ver_a_1");
  seedPlanner(session, projection);
  const pendingSave = deferred<Response>();
  let saveCallCount = 0;
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = new URL(String(input)).pathname;
    if (path.endsWith("/providers/status")) return jsonResponse(providerStatus());
    if (path.endsWith("/map/config")) return jsonResponse({ provider: "amap", enabled: false, jsApiKey: "" });
    if (path.endsWith("/agent/sessions") && (init?.method ?? "GET") === "GET") return jsonResponse({ sessions: [] });
    if (path.endsWith("/agent/sessions/current")) return jsonResponse(session);
    if (path.includes("/versions/saved")) return jsonResponse({ savedVersions: [] });
    if (path.endsWith("/save-active")) {
      saveCallCount += 1;
      return pendingSave.promise;
    }
    return jsonResponse({}, 404);
  }));

  render(<AppShell />);
  const overviewTab = await screen.findByRole("tab", { name: "行程总览" });
  fireEvent.click(screen.getByRole("tab", { name: "行程对比" }));
  fireEvent.keyDown(overviewTab, { key: "ArrowRight" });
  await waitFor(() => expect(saveCallCount).toBe(1));
  expect(overviewTab.getAttribute("aria-selected")).toBe("true");

  pendingSave.resolve(jsonResponse({ code: "SIMPLE_DIRECTION_SAVE_FAILED", message: "数据库写入失败：proposal stale" }, 409));
  await waitFor(() => expect(
    screen.getAllByRole("alert").some((alert) => alert.textContent?.includes("数据库写入失败：proposal stale"))
  ).toBe(true));
  expect(overviewTab.getAttribute("aria-selected")).toBe("true");
  expect(saveCallCount).toBe(1);
});

test("Agent auto-navigation preserves the new card but cannot bypass a failing save", async () => {
  const planA = itinerary("a", "高校集中方向");
  const planB = itinerary("b", "公园夜游方向");
  const projectionA = direction(
    planA,
    "direction_a",
    "assistant_initial",
    "confirm_a_0",
    true,
    "ver_a_1"
  );
  const initialCarrier = carrier("assistant_initial", 1, [projectionA], "replace");
  let session = agentSession([initialCarrier], planA, "ver_a_1");
  seedPlanner(session, projectionA);
  const requestBodies: Array<Record<string, unknown>> = [];
  let saveCallCount = 0;
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = new URL(String(input)).pathname;
    const method = init?.method ?? "GET";
    if (path.endsWith("/providers/status")) return jsonResponse(providerStatus());
    if (path.endsWith("/map/config")) return jsonResponse({ provider: "amap", enabled: false, jsApiKey: "" });
    if (path.endsWith("/agent/sessions") && method === "GET") return jsonResponse({ sessions: [] });
    if (path.endsWith("/agent/sessions/current")) return jsonResponse(session);
    if (path.includes("/versions/saved")) return jsonResponse({ savedVersions: [] });
    if (path.endsWith(`/agent/sessions/${SESSION_ID}/messages/stream`) && method === "POST") {
      return jsonResponse({ detail: "stream disabled" }, 404);
    }
    if (path.endsWith(`/agent/sessions/${SESSION_ID}/messages`) && method === "POST") {
      const body = JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>;
      requestBodies.push(body);
      const projectionB = direction(planB, "direction_b", "assistant_new_b", "confirm_b_0");
      const assistant = carrier("assistant_new_b", 3, [projectionB], "append");
      const response: AgentMessageResponse = {
        userTurn: turn("user_new_b", "user", 2, "生成其他方向"),
        assistantTurn: assistant,
        itinerary: planA,
        version: null,
        pendingPoiCandidates: [],
        warnings: []
      };
      session = agentSession([...session.turns, response.userTurn, response.assistantTurn], planA, "ver_a_1");
      return jsonResponse(response);
    }
    if (path.endsWith("/save-active")) {
      saveCallCount += 1;
      return jsonResponse({ code: "SIMPLE_DIRECTION_SAVE_FAILED", message: "自动保存失败：active version stale" }, 409);
    }
    return jsonResponse({}, 404);
  }));

  render(<AppShell />);
  const input = await screen.findByLabelText("Agent 对话文本");
  fireEvent.change(input, { target: { value: "生成其他方向" } });
  fireEvent.keyDown(input, { key: "Enter" });

  await waitFor(() => expect(plannerStore.getSnapshot().comparisonPreview.plans.map((plan) => plan.proposalId)).toEqual([
    "direction_a",
    "direction_b"
  ]));
  await waitFor(() => expect(
    screen.getAllByRole("alert").some((alert) => alert.textContent?.includes("自动保存失败：active version stale"))
  ).toBe(true));
  expect(screen.getByRole("tab", { name: "行程总览" }).getAttribute("aria-selected")).toBe("true");
  expect(saveCallCount).toBe(1);
  expect((requestBodies[0].context as Record<string, unknown>).currentUserMessage).toBe("生成其他方向");
  expect(agentViewContext(requestBodies[0])).toMatchObject({
    activeView: "overview",
    editingProposal: { proposalId: "direction_a", activeVersionId: "ver_a_1" }
  });
});

function direction(
  plan: ItineraryPlan,
  proposalId: string,
  sourceAssistantTurnId: string,
  choiceId: string,
  isAdopted = false,
  activeVersionId: string | null = null
): ComparisonPlanProjection {
  return {
    planningSelectionRootTurnId: ROOT_ID,
    rootPortfolioId: PORTFOLIO_ID,
    proposalId,
    sourceAssistantTurnId,
    choiceId,
    workflowMode: "simple_direction_v1",
    status: "complete",
    isPartial: false,
    isAdopted,
    adoptionReady: true,
    confirmationPassed: true,
    requiredPlanningDayNumbers: [1],
    explicitRestDayNumbers: [],
    uncoveredDayNumbers: [],
    activeVersionId,
    expectedBaseVersionId: activeVersionId,
    title: plan.title,
    days: plan.days,
    pendingSlots: [],
    routeEvidence: [],
    routeStatus: "route_not_required",
    routeExpectedLegCount: 0,
    routeVerifiedLegCount: 0,
    budgetSummary: "中等预算 · 预算待核验",
    routeSummary: "路线无需核验",
    nextAction: "confirm_edit",
    nextActionLabel: "确认编辑",
    tradeoffSummary: "方向差异",
    colorKey: "ocean"
  };
}

function carrier(
  id: string,
  turnIndex: number,
  projections: ComparisonPlanProjection[],
  updateMode: "append" | "replace",
  status: ConversationTurn["status"] = "active"
): ConversationTurn {
  return turn(id, "assistant", turnIndex, status === "internal_capability" ? "" : "方向已生成。", {
    status,
    comparisonProjections: projections,
    comparisonProjectionUpdateMode: updateMode,
    choiceOptions: projections.map((projection) => ({
      id: projection.choiceId,
      action: "select_plan_proposal",
      kind: "plan_proposal",
      label: "确认编辑",
      comparisonProjection: projection
    }))
  });
}

function confirmationResponse(
  sequence: number,
  sourceAssistantTurnId: string,
  choiceId: string,
  plan: ItineraryPlan,
  versionId: string
): AgentMessageResponse {
  return {
    userTurn: turn(`user_confirm_${sequence}`, "user", sequence * 10, "确认编辑", {
      itineraryVersionId: versionId,
      structuredChoiceTrace: {
        sourceAssistantTurnId,
        resolvedChoiceId: choiceId,
        executionStatus: "succeeded"
      }
    }),
    assistantTurn: turn(
      `assistant_confirm_${sequence}`,
      "assistant",
      sequence * 10 + 1,
      "已进入该方向的可编辑时间轴。",
      { itineraryVersionId: versionId }
    ),
    itinerary: plan,
    version: { id: versionId, versionNumber: sequence, sourceType: "agent" },
    pendingPoiCandidates: [],
    warnings: []
  };
}

function itinerary(seed: string, title: string): ItineraryPlan {
  return {
    id: `plan_${seed}`,
    title,
    city: "北京",
    templateType: "simple_open",
    budgetEstimate: 0,
    budgetDeltaExplanation: "预算待核验",
    decisionRationale: "方向草案",
    status: "ready",
    days: [{
      id: `day_${seed}`,
      dayNumber: 1,
      weatherSummary: "天气待查询",
      riskSummary: "风险待核验",
      totalEstimatedCost: 0,
      segments: [{
        id: `segment_${seed}`,
        startTime: "09:00",
        endTime: "10:30",
        kind: "activity",
        poi: {
          id: `poi_${seed}`,
          amapId: `amap_${seed}`,
          name: `${title}站点`,
          city: "北京",
          category: "landmark",
          latitude: 39.9,
          longitude: 116.4,
          source: "amap-place-search",
          confidence: 0.9
        },
        transportMode: "transit",
        estimatedCost: 0,
        notes: ""
      }]
    }],
    routeOptions: [],
    weatherSignals: [],
    trafficCrowdingSignals: [],
    ticketLookupResults: []
  };
}

function twoDayItinerary(seed: string, title: string): ItineraryPlan {
  const plan = itinerary(seed, title);
  const firstDay = plan.days[0];
  return {
    ...plan,
    days: [
      firstDay,
      {
        ...firstDay,
        id: `day_${seed}_2`,
        dayNumber: 2,
        segments: firstDay.segments.map((segment) => ({
          ...segment,
          id: `segment_${seed}_2`,
          poi: {
            ...segment.poi,
            id: `poi_${seed}_2`,
            amapId: `amap_${seed}_2`,
            name: `${title}第二天站点`,
            latitude: 40.2,
            longitude: 116.2
          }
        }))
      }
    ]
  };
}

function turn(
  id: string,
  role: ConversationTurn["role"],
  turnIndex: number,
  content: string,
  overrides: Partial<ConversationTurn> = {}
): ConversationTurn {
  return {
    id,
    role,
    content,
    turnIndex,
    status: "active",
    createdAt: CREATED_AT,
    updatedAt: CREATED_AT,
    ...overrides
  };
}

function agentSession(
  turns: ConversationTurn[],
  itineraryPlan: ItineraryPlan | null,
  activeVersionId: string | null
): AgentSession {
  return {
    sessionId: SESSION_ID,
    status: "active",
    city: "北京",
    title: "北京方向探索",
    activePlanId: itineraryPlan?.id ?? "plan_simple_direction",
    activeVersionId,
    turns,
    itinerary: itineraryPlan,
    pendingPoiCandidates: []
  };
}

function seedPlanner(session: AgentSession, adopted?: ComparisonPlanProjection) {
  let preview = comparisonPreviewFromTurns(session.turns, createComparisonPreviewState());
  if (adopted && session.activeVersionId) {
    preview = markComparisonPlanAdopted(preview, adopted.proposalId, session.activeVersionId);
  }
  plannerStore.setState({
    selectedCity: "北京",
    agentSession: session,
    agentSessions: [sessionSummary(session)],
    conversationTurns: session.turns,
    activeVersionId: session.activeVersionId ?? null,
    itineraryPlan: session.itinerary,
    comparisonPreview: preview,
    selectedDayNumber: 1,
    selectedSegmentId: session.itinerary?.days[0]?.segments[0]?.id ?? null,
    preferenceCard: null,
    preferenceMemory: null,
    pendingPoiCandidates: [],
    candidateMapPois: [],
    selectedMapPoi: null,
    selectedRouteOptionId: null,
    previewRouteOptionId: null,
    routeWarnings: []
  });
}

function replaceActiveItinerary(plan: ItineraryPlan, versionId: string) {
  const snapshot = plannerStore.getSnapshot();
  plannerStore.setState({
    activeVersionId: versionId,
    itineraryPlan: plan,
    agentSession: snapshot.agentSession
      ? { ...snapshot.agentSession, activePlanId: plan.id, activeVersionId: versionId, itinerary: plan }
      : null
  });
}

function resetPlannerStore() {
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
    selectedRouteOptionId: null,
    previewRouteOptionId: null,
    routeWarnings: [],
    itineraryAgentContext: null,
    timelineCopyText: "",
    lastPlanningRun: null
  });
}

function card(proposalId: string): HTMLElement {
  const element = document.querySelector(`[data-proposal-id="${proposalId}"]`);
  if (!(element instanceof HTMLElement)) throw new Error(`missing comparison card ${proposalId}`);
  return element;
}

function selectedChoice(body: Record<string, unknown>) {
  const context = body.context as Record<string, unknown>;
  return context.selectedAgentChoice as { sourceAssistantTurnId: string; choiceId: string };
}

function agentViewContext(body: Record<string, unknown>) {
  const context = body.context as Record<string, unknown>;
  return context.viewContext as Record<string, unknown>;
}

function sessionSummary(session: AgentSession) {
  return {
    sessionId: session.sessionId,
    status: session.status,
    city: session.city,
    title: session.title,
    activePlanId: session.activePlanId,
    activeVersionId: session.activeVersionId,
    turnCount: session.turns.length,
    updatedAt: CREATED_AT,
    createdAt: CREATED_AT
  };
}

function providerStatus() {
  return { mode: "mock", default: [], mock: [] };
}

function jsonResponse(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "Content-Type": "application/json" }
  });
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolver) => {
    resolve = resolver;
  });
  return { promise, resolve };
}

import { afterEach, expect, test, vi } from "vitest";
import { apiClient } from "../../src/services/apiClient";

afterEach(() => {
  vi.restoreAllMocks();
});

test("patchItinerary uses versioned patch endpoint with explicit baseVersionId", async () => {
  const fetchHandler = async (_url: RequestInfo | URL, _init?: RequestInit): Promise<Response> =>
    new Response(
      JSON.stringify({
        itinerary: { id: "plan_boundary", title: "Boundary", city: "北京", days: [], routeOptions: [] },
        patch: { id: "patch_boundary", validationStatus: "accepted" },
        version: { id: "ver_next", versionNumber: 2, sourceType: "manual" },
        validationErrors: [],
        pendingPoiCandidates: []
      }),
      { status: 200, headers: { "Content-Type": "application/json" } }
    );
  const fetchMock = vi.fn(fetchHandler);
  vi.stubGlobal("fetch", fetchMock);

  await apiClient.patchItinerary("plan_boundary", {
    sourceType: "manual",
    baseVersionId: "ver_current",
    operations: [{ op: "replace_trip_title", value: "New title" }]
  });

  const [url, init] = fetchMock.mock.calls[0];
  const body = JSON.parse(String(init?.body ?? "{}")) as { baseVersionId?: string };

  expect(String(url)).toBe("http://localhost:8000/api/itineraries/plan_boundary/patch");
  expect(init?.method).toBe("POST");
  expect(body.baseVersionId).toBe("ver_current");
  expect(fetchMock.mock.calls).not.toContainEqual([
    "http://localhost:8000/api/itineraries/plan_boundary",
    expect.objectContaining({ method: "PATCH" })
  ]);
});

test("map POI resolve response normalizes missing arrays", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () =>
      new Response(
        JSON.stringify({
          resolved: [{ query: "故宫", status: "accepted", poi: mapPoiFixture("poi_1", "故宫博物院") }],
          pending: [{ candidateRecordId: "pending_1", query: "国贸", reason: "multiple_matches" }]
        }),
        { status: 200, headers: { "Content-Type": "application/json" } }
      )
    )
  );

  const response = await apiClient.resolveMapPois({
    sessionId: "session_1",
    city: "北京",
    queries: [{ name: "故宫" }]
  });

  expect(response.resolved).toHaveLength(1);
  expect(response.pending).toHaveLength(1);
  expect(response.pending[0].candidates).toEqual([]);
});

test("resumeAgentTurn posts to failed turn resume endpoint", async () => {
  const fetchHandler = async (_url: RequestInfo | URL, _init?: RequestInit): Promise<Response> =>
    new Response(
      JSON.stringify({
        userTurn: turnFixture("turn_user", "user", "active"),
        assistantTurn: turnFixture("turn_next", "assistant", "active"),
        pendingPoiCandidates: [],
        warnings: [],
        planningSteps: [],
        toolEvents: []
      }),
      { status: 200, headers: { "Content-Type": "application/json" } }
    );
  const fetchMock = vi.fn(fetchHandler);
  vi.stubGlobal("fetch", fetchMock);

  const response = await apiClient.resumeAgentTurn("session_1", "turn_failed");

  const [url, init] = fetchMock.mock.calls[0];
  expect(String(url)).toBe("http://localhost:8000/api/agent/sessions/session_1/turns/turn_failed/resume");
  expect(init?.method).toBe("POST");
  expect(response.pendingPoiCandidates).toEqual([]);
});

test("saveActiveDirection sends identity only and accepts a server-reloaded projection", async () => {
  const projection = {
    planningSelectionRootTurnId: "root_1",
    rootPortfolioId: "portfolio_1",
    proposalId: "direction_a",
    sourceAssistantTurnId: "assistant_saved_a",
    choiceId: "confirm_saved_a",
    workflowMode: "simple_direction_v1",
    status: "complete",
    isPartial: false,
    isAdopted: true,
    adoptionReady: true,
    activeVersionId: "ver_edited",
    expectedBaseVersionId: "ver_edited",
    title: "已编辑方向",
    days: [],
    pendingSlots: [],
    routeEvidence: [],
    budgetSummary: "中等预算",
    routeSummary: "路线已核验",
    nextAction: "confirm_edit",
    nextActionLabel: "确认编辑",
    tradeoffSummary: ""
  };
  const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) =>
    new Response(JSON.stringify({
      proposalId: "direction_a",
      activeVersionId: "ver_edited",
      comparisonProjection: projection,
      saved: true,
      unchanged: false
    }), { status: 200, headers: { "Content-Type": "application/json" } })
  );
  vi.stubGlobal("fetch", fetchMock);

  const response = await apiClient.saveActiveDirection("session_1", "direction_a", {
    baseVersionId: "ver_edited",
    planningSelectionRootTurnId: "root_1",
    rootPortfolioId: "portfolio_1"
  });

  const [url, init] = fetchMock.mock.calls[0];
  expect(String(url)).toBe(
    "http://localhost:8000/api/agent/sessions/session_1/directions/direction_a/save-active"
  );
  expect(init?.method).toBe("POST");
  expect(JSON.parse(String(init?.body))).toEqual({
    baseVersionId: "ver_edited",
    planningSelectionRootTurnId: "root_1",
    rootPortfolioId: "portfolio_1"
  });
  expect(response.comparisonProjection).toEqual(projection);
});

function mapPoiFixture(id: string, name: string) {
  return {
    id,
    name,
    type: "风景名胜",
    city: "北京市",
    district: "东城区",
    address: "北京",
    longitude: 116.397026,
    latitude: 39.918058,
    category: "scenic",
    source: "amap-place-search",
    sourceNote: "高德 WebService POI 搜索",
    confidence: 0.95,
    photos: []
  };
}

function turnFixture(id: string, role: "user" | "assistant", status: string) {
  return {
    id,
    role,
    content: role === "user" ? "国庆安排北京一天" : "已继续规划。",
    turnIndex: role === "user" ? 1 : 2,
    status,
    planningSteps: [],
    toolEvents: [],
    createdAt: "2026-07-01T00:00:00Z",
    updatedAt: "2026-07-01T00:00:00Z"
  };
}

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { AppShell } from "../../src/components/AppShell";
import { PlannerMap } from "../../src/components/map/PlannerMap";
import { buildRouteLegColorMap, routeLegColor } from "../../src/components/timeline/routeVisuals";
import { plannerStore } from "../../src/state/plannerStore";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

test("generates a map-linked itinerary and syncs timeline selection", async () => {
  let agentMessageCount = 0;
  const mapPolylineOptions: Array<Record<string, unknown>> = [];
  const mapMarkerOptions: Array<Record<string, unknown>> = [];
  const { markerConstructor } = setupAmapRuntime(mapPolylineOptions, mapMarkerOptions, true);
  const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) {
      return jsonResponse({ mode: "mock", default: [], mock: [] });
    }
    if (path.endsWith("/map/config")) {
      return jsonResponse({ provider: "amap", enabled: true, jsApiKey: "web-key" });
    }
    if (path.endsWith("/preferences/extract")) {
      return jsonResponse({ summaryCard: preferenceCard() });
    }
    if (path.endsWith("/agent/sessions")) {
      return jsonResponse(agentSession());
    }
    if (path.endsWith("/agent/sessions/sess_us2/messages")) {
      agentMessageCount += 1;
      return jsonResponse(agentMessageResponse(agentMessageCount));
    }
    if (path.endsWith("/itineraries/plan_us2/patch") && init?.method === "POST") {
      const body = JSON.parse(String(init.body ?? "{}"));
      const operation = body.operations?.[0];
      if (operation?.op === "remove_segment") {
        return jsonResponse({
          itinerary: { ...itineraryPlan("route_1_taxi", String(operation.segmentId)), title: "北京故宫慢游" },
          patch: { id: "patch_remove", validationStatus: "accepted" },
          version: { id: "ver_remove", versionNumber: 4, sourceType: "manual" },
          pendingPoiCandidates: [],
          validationErrors: []
        });
      }
      return jsonResponse({
        itinerary: {
          ...itineraryPlan(),
          title: String(operation?.value ?? "北京故宫慢游")
        },
        patch: { id: "patch_title", validationStatus: "accepted" },
        version: { id: "ver_title", versionNumber: 2, sourceType: "manual" },
        pendingPoiCandidates: [],
        validationErrors: []
      });
    }
    if (path.endsWith("/itineraries/plan_us2/routes/route_1_transit/select") && init?.method === "POST") {
      return jsonResponse({
        itinerary: itineraryPlan("route_1_transit"),
        patch: { id: "patch_route", validationStatus: "accepted" },
        version: { id: "ver_route", versionNumber: 3, sourceType: "manual" },
        pendingPoiCandidates: [],
        validationErrors: []
      });
    }
    if (path.endsWith("/itineraries/plan_us2") && init?.method === "PATCH") {
      return jsonResponse({
        plan: {
          ...itineraryPlan(),
          days: [
            {
              ...itineraryPlan().days[0],
              segments: itineraryPlan().days[0].segments.map((segment) => ({
                ...segment,
                transportMode: "self_drive"
              }))
            }
          ],
          routeOptions: itineraryPlan().routeOptions.map((route) => ({
            ...route,
            transportMode: "self_drive"
          }))
        }
      });
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);

  fireEvent.change(screen.getByLabelText("Agent 对话文本"), {
    target: { value: "北京 故宫博物院 拍照 预算 3000 元" }
  });
  fireEvent.click(screen.getByText("发送给 Agent"));

  await waitFor(() => expect(screen.getByText("北京 故宫博物院 拍照 预算 3000 元")).toBeTruthy());
  expect((screen.getByLabelText("Agent 对话文本") as HTMLTextAreaElement).value).toBe("");
  await waitFor(() => expect(screen.getByText("已生成北京 1 日行程。")).toBeTruthy());
  await waitFor(() => expect(screen.getAllByText("故宫博物院").length).toBeGreaterThan(0));
  expect(mapMarkerOptions).toHaveLength(0);
  expect(markerConstructor).not.toHaveBeenCalled();
  expect(screen.getByLabelText("选择 故宫博物院")).toBeTruthy();
  expect(screen.getByLabelText("选择 景山公园")).toBeTruthy();
  expect(screen.getByLabelText("选择 北海公园")).toBeTruthy();
  expect(screen.queryByLabelText("选择 天坛公园")).toBeNull();
  await waitFor(() => {
    const routePolylines = mapPolylineOptions.filter((options) => options.strokeWeight !== 9);
    const plan = itineraryPlan();
    const firstTimelineRoute = plan.routeOptions.find((route) => route.id === "route_1_taxi");
    const secondTimelineRoute = plan.routeOptions.find((route) => route.id === "route_2_transit");
    const staleRoute = plan.routeOptions.find((route) => route.id === "route_stale_reverse");
    const crossDayRoute = plan.routeOptions.find((route) => route.id === "route_cross_day");
    const colorMap = buildRouteLegColorMap(plan.days, plan.routeOptions);
    expect(routePolylines).toHaveLength(2);
    expect(routePolylines.map((options) => options.path)).toEqual([firstTimelineRoute?.polyline, secondTimelineRoute?.polyline]);
    expect(routePolylines.map((options) => options.path)).not.toContainEqual(staleRoute?.polyline);
    expect(routePolylines.map((options) => options.path)).not.toContainEqual(crossDayRoute?.polyline);
    expect(routePolylines.map((options) => options.strokeColor)).toEqual([
      routeLegColor(firstTimelineRoute!, colorMap),
      routeLegColor(secondTimelineRoute!, colorMap)
    ]);
    expect(routePolylines.some((options) => options.strokeWeight === 8 && options.strokeOpacity === 0.98)).toBe(true);
  });
  const clickTimelineSegment = (name: string) => {
    const target = screen.getAllByTestId("timeline-segment-name").find((item) => item.textContent === name);
    expect(target).toBeTruthy();
    fireEvent.click(target!);
  };
  clickTimelineSegment("天坛公园");
  await waitFor(() => expect(screen.getByText("当前选中：天坛公园")).toBeTruthy());
  await waitFor(() => expect(screen.getByLabelText("选择 天坛公园").className).toContain("selected"));
  expect(markerConstructor).not.toHaveBeenCalled();
  clickTimelineSegment("故宫博物院");
  await waitFor(() => expect(screen.getByText("当前选中：故宫博物院")).toBeTruthy());
  expect(screen.queryByText("以下 POI 需要确认，当前不会写入最终行程。")).toBeNull();
  expect(screen.getByText("当天路线 8.0 km · 27 分钟")).toBeTruthy();
  expect(screen.getByText("故宫博物院 → 景山公园")).toBeTruthy();
  expect(screen.getByText("景山公园 → 北海公园")).toBeTruthy();
  expect(screen.getByText("6.8 km · 15 分钟 · 打车 · 来源：高德地图")).toBeTruthy();
  expect(screen.queryByText(/mock-map-provider/)).toBeNull();
  expect(screen.getAllByText(/来源：高德地图/).length).toBeGreaterThan(0);
  expect(screen.queryByText(/amap-webservice/)).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "展开风险" }));
  const weatherReminder = screen.getByText("天气").closest("details") as HTMLDetailsElement;
  expect(weatherReminder.open).toBe(false);
  expect(screen.getByText("晴，适合拍照")).toBeTruthy();

  clickTimelineSegment("故宫博物院");
  expect(screen.getByText("当前选中：故宫博物院")).toBeTruthy();
  const fastestRoute = screen.getAllByText("驾车/打车").find((item) => item.closest("article")) as HTMLElement;
  const fastestRouteCard = fastestRoute.closest("article") as HTMLElement;
  expect(within(fastestRouteCard).getByText("当前路线")).toBeTruthy();
  expect(within(fastestRouteCard).getByText("15 分钟 · 6.8 km · ¥32")).toBeTruthy();
  expect(screen.getByText("公交/地铁无可用路线，暂用骑行/步行估算。")).toBeTruthy();
  const transitRoute = screen.getAllByText("公交/地铁").find((item) => item.closest("article")) as HTMLElement;
  const transitRouteCard = transitRoute.closest("article") as HTMLElement;
  fireEvent.click(transitRoute);
  expect(plannerStore.getSnapshot().previewRouteOptionId).toBe("route_1_transit");
  await waitFor(() => {
    const plan = itineraryPlan();
    const colorMap = buildRouteLegColorMap(plan.days, plan.routeOptions);
    const previewRoute = plan.routeOptions.find((route) => route.id === "route_1_transit");
    const previewPath = JSON.stringify(previewRoute?.polyline);
    const previewPolyline = mapPolylineOptions.find(
      (options) => options.strokeWeight === 9 && JSON.stringify(options.path) === previewPath
    );
    expect(previewPolyline?.strokeColor).toBe(routeLegColor(previewRoute!, colorMap));
    expect(previewPolyline?.strokeColor).not.toBe("#ff8a00");
  });
  expect(screen.getByText("故宫博物院 → 景山公园")).toBeTruthy();
  expect(screen.getByText("景山公园 → 北海公园")).toBeTruthy();
  await waitFor(() => expect(within(transitRouteCard).getByText("18 分钟 · 1.9 km · ¥4")).toBeTruthy());
  fireEvent.click(within(transitRouteCard).getByText("使用此路线"));
  await waitFor(() => {
    const selectCall = fetchMock.mock.calls.find((call) => String(call[0]).endsWith("/itineraries/plan_us2/routes/route_1_transit/select"));
    expect(selectCall).toBeTruthy();
  });
  await waitFor(() => expect(screen.getByText("已保存路线选择。")).toBeTruthy());
  expect(screen.queryByText("使用此路线")).toBeNull();
  expect(fetchMock.mock.calls.some((call) => String(call[0]).endsWith("/itineraries/plan_us2") && call[1]?.method === "PATCH")).toBe(false);
  expect(screen.getAllByText("展开路线").length).toBeGreaterThan(0);
  fireEvent.click(screen.getAllByText("展开路线")[0]);
  expect(screen.getAllByText("使用此路线").length).toBeGreaterThan(0);
  expect(screen.getAllByText("公交/地铁").length).toBeGreaterThan(0);
  clickTimelineSegment("北海公园");
  expect(screen.getByText("当前选中：北海公园")).toBeTruthy();

  fireEvent.click(screen.getAllByText("折叠")[0]);
  expect(screen.getAllByText(/已知路线 3\.1 km · 2\/2 路段已生成/).length).toBeGreaterThan(0);

  fireEvent.click(screen.getByRole("button", { name: "北京地图行程草案" }));
  fireEvent.change(screen.getByLabelText("旅行标题输入"), { target: { value: "北京故宫慢游" } });
  fireEvent.click(screen.getByText("确认"));
  await waitFor(() => expect(screen.getAllByText("北京故宫慢游").length).toBeGreaterThan(0));

  vi.spyOn(window, "confirm").mockReturnValue(true);
  fireEvent.click(screen.getByRole("button", { name: "删除当前景点" }));
  await waitFor(() => expect(screen.getAllByText("已删除：北海公园").length).toBeGreaterThan(0));
  expect(screen.queryByText("北海公园")).toBeNull();
  expect(plannerStore.getSnapshot().selectedSegmentId).toBe("seg_2");
  expect(plannerStore.getSnapshot().candidateMapPois).toEqual([]);
  expect(plannerStore.getSnapshot().selectedMapPoi).toBeNull();
  expect(plannerStore.getSnapshot().selectedRouteOptionId).toBeNull();
  expect(plannerStore.getSnapshot().previewRouteOptionId).toBeNull();
  const mapRemoveCall = fetchMock.mock.calls.find(
    (call) => String(call[0]).endsWith("/itineraries/plan_us2/patch") && String(call[1]?.body).includes("map_remove_segment")
  );
  expect(mapRemoveCall).toBeTruthy();
  const mapRemoveBody = JSON.parse(String(mapRemoveCall?.[1]?.body));
  expect(mapRemoveBody.planningContext.patchIntent).toBe("map_remove_segment");
  expect(mapRemoveBody.planningContext.toolRefreshPolicy).toEqual({ route: "touched_pairs_only" });

  fireEvent.click(screen.getByRole("button", { name: "删除 景山公园" }));
  await waitFor(() => expect(screen.getByText("已删除景点/活动，路线已重新规划或标记为待重新规划。")).toBeTruthy());
  expect(screen.queryByText("景山公园")).toBeNull();
  expect(fetchMock.mock.calls.some((call) => String(call[0]).endsWith("/itineraries/plan_us2/patch") && String(call[1]?.body).includes("remove_segment"))).toBe(true);

  fireEvent.change(screen.getByLabelText("Agent 对话文本"), {
    target: { value: "把当前安排再放松一点" }
  });
  fireEvent.click(screen.getByText("发送给 Agent"));

  await waitFor(() => {
    const messageCalls = fetchMock.mock.calls.filter((call) => String(call[0]).endsWith("/agent/sessions/sess_us2/messages"));
    expect(messageCalls.length).toBeGreaterThanOrEqual(2);
    const secondBody = JSON.parse(String(messageCalls[1][1]?.body));
    expect(secondBody.context.activeVersionId).toBe("ver_remove");
    expect(secondBody.context.timelineContext.activeVersionId).toBe("ver_remove");
    expect(secondBody.context.timelineContext.itineraryPlan.title).toBe("北京故宫慢游");
    expect(secondBody.context.itineraryPlan.title).toBe("北京故宫慢游");
    expect(secondBody.context.timelineContext.selectedSegment.poi.name).toBeTruthy();
  });
  expect(screen.queryByText("生成 Agent 上下文")).toBeNull();
}, 10000);

test("partial itinerary map displays confirmed POIs but never invents a marker for an empty slot", async () => {
  setupAmapRuntime([], [], true);
  vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL) => {
    if (String(url).endsWith("/map/config")) {
      return jsonResponse({ provider: "amap", enabled: true, jsApiKey: "web-key" });
    }
    return jsonResponse({}, 404);
  }));
  const base = itineraryPlan();
  const partial = {
    ...base,
    status: "partial",
    days: [{
      ...base.days[0],
      segments: base.days[0].segments.slice(0, 2),
      pendingSlots: [{
        id: "pending_walk",
        planningSlotId: "walk-slot",
        briefId: "local",
        poolId: "walk-pool",
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
      }]
    }]
  };

  render(
    <PlannerMap
      plan={partial}
      selectedSegmentId="seg_1"
      onSelectSegment={() => undefined}
      city="北京"
    />
  );

  await waitFor(() => expect(screen.getByLabelText("选择 故宫博物院")).toBeTruthy());
  expect(screen.getByLabelText("选择 景山公园")).toBeTruthy();
  expect(screen.queryByLabelText(/街区漫步/)).toBeNull();
});

test("map never draws failed or needs-refresh route evidence even when a legacy polyline exists", async () => {
  const mapPolylineOptions: Array<Record<string, unknown>> = [];
  setupAmapRuntime(mapPolylineOptions, [], true);
  vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL) => {
    if (String(url).endsWith("/map/config")) {
      return jsonResponse({ provider: "amap", enabled: true, jsApiKey: "web-key" });
    }
    return jsonResponse({}, 404);
  }));
  const base = itineraryPlan();
  const unsafeRoutes = [
    {
      ...routeOption("route_needs_refresh", "seg_1", "seg_2", "poi_1", "poi_2", "transit", "待刷新", 1200, 720, 4, "route_needs_refresh"),
      status: "needs_refresh"
    },
    {
      ...routeOption("route_failed", "seg_2", "seg_3", "poi_2", "poi_3", "transit", "失败", 1400, 840, 4, "route_failed"),
      status: "failed"
    }
  ];

  render(
    <PlannerMap
      plan={{ ...base, routeOptions: unsafeRoutes }}
      selectedSegmentId="seg_1"
      onSelectSegment={() => undefined}
      city="北京"
    />
  );

  await waitFor(() => expect(screen.getByLabelText("选择 故宫博物院")).toBeTruthy());
  const routePolylines = mapPolylineOptions.filter((options) => options.strokeWeight !== 9);
  expect(routePolylines).toHaveLength(0);
});

test("map projection failures do not crash selected timeline POI rendering", async () => {
  const consoleWarn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
  const mapApi = {
    destroy: vi.fn(),
    getZoom: vi.fn(() => 13),
    lngLatToContainer: vi.fn(() => {
      throw new Error("Cannot read properties of undefined (reading 'getOptions')");
    }),
    on: vi.fn(),
    panTo: vi.fn(),
    setCenter: vi.fn(),
    setFitView: vi.fn(),
    zoomIn: vi.fn(),
    zoomOut: vi.fn(),
    setPitch: vi.fn(),
    setRotation: vi.fn(),
    setZoomAndCenter: vi.fn()
  };
  const markerConstructor = vi.fn(() => {
    throw new Error("AMap Marker should not be constructed for timeline POI pins");
  });
  vi.stubGlobal("AMap", {
    Map: vi.fn(() => mapApi),
    Marker: markerConstructor,
    Polyline: vi.fn(() => ({ setMap: vi.fn(), setOptions: vi.fn() }))
  });
  vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/map/config")) {
      return jsonResponse({ provider: "amap", enabled: true, jsApiKey: "web-key" });
    }
    return jsonResponse({}, 404);
  }));

  render(<PlannerMap plan={itineraryPlan()} selectedSegmentId="seg_1" onSelectSegment={() => undefined} city="北京" />);

  await waitFor(() => expect(screen.getByText("当前选中：故宫博物院")).toBeTruthy());
  await waitFor(() => expect(consoleWarn).toHaveBeenCalledWith("AMap lngLatToContainer failed", expect.any(Error)));
  const marker = screen.getByLabelText("选择 故宫博物院");
  expect(marker.style.visibility).toBe("hidden");
  expect(marker.dataset.projected).toBe("false");
  expect(markerConstructor).not.toHaveBeenCalled();
});

function setupAmapRuntime(
  polylineOptions: Array<Record<string, unknown>>,
  markerOptions: Array<Record<string, unknown>> = [],
  throwOnMarker = false
) {
  const mapInstance = {
    destroy: vi.fn(),
    getZoom: vi.fn(() => 13),
    lngLatToContainer: vi.fn(() => ({ getX: () => 240, getY: () => 180 })),
    on: vi.fn(),
    panTo: vi.fn(),
    setCenter: vi.fn(),
    setFitView: vi.fn(),
    zoomIn: vi.fn(),
    zoomOut: vi.fn(),
    setPitch: vi.fn(),
    setRotation: vi.fn(),
    setZoomAndCenter: vi.fn()
  };
  const markerConstructor = vi.fn((options: Record<string, unknown>) => {
    if (throwOnMarker) {
      throw new Error("AMap Marker should not be constructed for timeline POI pins");
    }
    markerOptions.push(options);
    return { on: vi.fn(), setMap: vi.fn(), setOptions: vi.fn() };
  });
  vi.stubGlobal("AMap", {
    Map: vi.fn(() => mapInstance),
    Marker: markerConstructor,
    Polyline: vi.fn((options: Record<string, unknown>) => {
      polylineOptions.push(options);
      return {
        setMap: vi.fn(),
        setOptions: vi.fn((nextOptions: Record<string, unknown>) => {
          Object.assign(options, nextOptions);
        })
      };
    })
  });
  return { mapInstance, markerConstructor };
}

function routePolyline(id: string) {
  const polylines: Record<string, number[][]> = {
    route_stale_reverse: [[116.3901, 39.9301], [116.3972, 39.9163]],
    route_cross_day: [[116.3901, 39.9301], [116.4074, 39.9042]],
    route_1_walk: [[116.3972, 39.9163], [116.3975, 39.9239]],
    route_1_transit: [[116.3972, 39.9163], [116.3969, 39.9236]],
    route_1_taxi: [[116.3972, 39.9163], [116.402, 39.9236]],
    route_2_transit: [[116.3969, 39.9236], [116.3901, 39.9301]]
  };
  return polylines[id] ?? [[116.3972, 39.9163], [116.3969, 39.9236]];
}

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" }
  });
}

function agentSession() {
  return {
    sessionId: "sess_us2",
    status: "active",
    city: "北京",
    title: "北京 AI 行程",
    activePlanId: "plan_us2",
    activeVersionId: null,
    turns: [],
    itinerary: null,
    pendingPoiCandidates: []
  };
}

function agentMessageResponse(index: number) {
  const createdAt = "2026-06-10T10:00:00Z";
  return {
    userTurn: {
      id: `turn_user_${index}`,
      role: "user",
      content: "北京 故宫博物院 拍照 预算 3000 元",
      turnIndex: 1,
      status: "active",
      createdAt,
      updatedAt: createdAt
    },
    assistantTurn: {
      id: `turn_assistant_${index}`,
      role: "assistant",
      content: "已生成北京 1 日行程。",
      turnIndex: 2,
      status: "active",
      itineraryVersionId: `ver_us2_${index}`,
      createdAt,
      updatedAt: createdAt
    },
    itinerary: itineraryPlan(),
    version: { id: `ver_us2_${index}`, versionNumber: index, sourceType: "agent" },
    pendingPoiCandidates: [],
    warnings: []
  };
}

function preferenceCard() {
  return {
    id: "card_us2",
    profileId: "pref_us2",
    partySize: 1,
    travelerTypes: ["自由行"],
    budgetRange: "3000",
    pacePreference: "轻松",
    items: [],
    status: "draft"
  };
}

function itineraryPlan(selectedRouteId = "route_1_taxi", removedSegmentId = "") {
  const segments = [
    {
      id: "seg_1",
      startTime: "09:30",
      endTime: "11:30",
      kind: "activity",
      poi: {
        id: "poi_1",
        name: "故宫博物院",
        city: "北京",
        category: "attraction",
        latitude: 39.9163,
        longitude: 116.3972,
        source: "amap",
        confidence: 0.9
      },
      transportMode: "public_transit",
      estimatedCost: 30,
      notes: "门票/预约状态待票务查询确认"
    },
    {
      id: "seg_2",
      startTime: "12:30",
      endTime: "14:00",
      kind: "activity",
      poi: {
        id: "poi_2",
        name: "景山公园",
        city: "北京",
        category: "park",
        latitude: 39.9236,
        longitude: 116.3969,
        source: "amap",
        confidence: 0.74
      },
      transportMode: "public_transit",
      estimatedCost: 10,
      notes: "可作为故宫后续拍照点"
    },
    {
      id: "seg_3",
      startTime: "15:30",
      endTime: "17:00",
      kind: "activity",
      poi: {
        id: "poi_3",
        name: "北海公园",
        city: "北京",
        category: "park",
        latitude: 39.9255,
        longitude: 116.3895,
        source: "amap",
        confidence: 0.7
      },
      transportMode: "public_transit",
      estimatedCost: 10,
      notes: "湖边散步和补充拍照"
    }
  ].filter((segment) => segment.id !== removedSegmentId);
  return {
    id: "plan_us2",
    title: "北京地图行程草案",
    city: "北京",
    templateType: "custom",
    budgetEstimate: 3000,
    budgetDeltaExplanation: "测试预算",
    decisionRationale: "按识别 POI 生成空间顺序",
    status: "draft",
    days: [
      {
        id: "day_1",
        dayNumber: 1,
        weatherSummary: "晴，适合拍照",
        riskSummary: "早高峰可能拥挤",
        totalEstimatedCost: 30,
        segments
      },
      {
        id: "day_2",
        dayNumber: 2,
        weatherSummary: "多云，适合室内外切换",
        riskSummary: "午后景区可能拥挤",
        totalEstimatedCost: 20,
        segments: [
          {
            id: "seg_4",
            startTime: "09:30",
            endTime: "11:00",
            kind: "activity",
            poi: {
              id: "poi_4",
              name: "天坛公园",
              city: "北京",
              category: "park",
              latitude: 39.8822,
              longitude: 116.4066,
              source: "amap",
              confidence: 0.81
            },
            transportMode: "public_transit",
            estimatedCost: 10,
            notes: "第二天上午拍照点"
          }
        ]
      }
    ],
    routeOptions: [
      routeOption("route_stale_reverse", "seg_3", "seg_1", "poi_3", "poi_1", "taxi", "过期反向路线", 9999, 300, 80, selectedRouteId),
      routeOption("route_cross_day", "seg_3", "seg_4", "poi_3", "poi_4", "taxi", "跨天旧路线", 6000, 840, 40, selectedRouteId),
      routeOption(
        "route_1_walk",
        "seg_1",
        "seg_2",
        "poi_1",
        "poi_2",
        "walking",
        "步行",
        2100,
        1700,
        0,
        selectedRouteId,
        { userVisibleCaveat: "公交/地铁无可用路线，暂用骑行/步行估算。" }
      ),
      routeOption("route_1_transit", "seg_1", "seg_2", "poi_1", "poi_2", "transit", "公交/地铁", 1900, 1080, 4, selectedRouteId),
      routeOption("route_1_taxi", "seg_1", "seg_2", "poi_1", "poi_2", "taxi", "打车", 6800, 900, 32, selectedRouteId),
      routeOption("route_2_transit", "seg_2", "seg_3", "poi_2", "poi_3", "transit", "公交/地铁", 1200, 720, 4, selectedRouteId)
    ],
    weatherSignals: [
      {
        id: "weather_1",
        city: "北京",
        date: "2026-06-30",
        dailySummary: "晴，适合拍照",
        hourlyForecast: [{ hour: "09:00", condition: "sunny" }],
        riskLevel: "ideal",
        purposeImpactReason: "户外拍照条件较好",
        source: "mock-weather-provider",
        queriedAt: "2026-06-30T00:00:00Z"
      }
    ],
    trafficCrowdingSignals: [
      {
        id: "traffic_1",
        routeOptionId: "route_1",
        realDataAvailable: false,
        crowdingLevel: "medium",
        estimatedReason: "工作日早高峰估算",
        recommendedDepartureAdjustment: "提前 20 分钟出发",
        source: "mock-traffic-provider",
        queriedAt: "2026-06-30T00:00:00Z"
      }
    ],
    ticketLookupResults: []
  };
}

function routeOption(
  id: string,
  fromSegmentId: string,
  toSegmentId: string,
  fromPoiId: string,
  toPoiId: string,
  mode: string,
  label: string,
  distanceMeters: number,
  durationSeconds: number,
  costAmount: number,
  selectedRouteId: string,
  providerPayload: Record<string, unknown> = {}
) {
  return {
    id,
    fromSegmentId,
    toSegmentId,
    fromPoiId,
    toPoiId,
    provider: "amap-webservice",
    mode,
    label,
    isSelected: id === selectedRouteId,
    sortOrder: mode === "walking" ? 1 : mode === "transit" ? 2 : 3,
    transportMode: mode,
    distanceMeters,
    durationSeconds,
    durationMinutes: Math.round(durationSeconds / 60),
    costAmount,
    costCurrency: "CNY",
    costEstimate: costAmount,
    crowdingRisk: mode === "transit" ? "medium" : "low",
    source: "amap-webservice",
    polyline: routePolyline(id),
    steps: [],
    providerPayload,
    error: null,
    queriedAt: "2026-06-10T10:00:00Z"
  };
}

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { cleanup, createEvent, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { AppShell } from "../../src/components/AppShell";
import { DailyTimeline, formatTimelineForCopy } from "../../src/components/timeline/DailyTimeline";
import { buildItineraryAgentContext, calculateTripTotals, createEditableDays } from "../../src/components/timeline/itineraryWorkspace";
import { isRouteAnchorSegment } from "../../src/components/timeline/routeAnchors";
import { buildRouteLegColorMap, routeLegColor } from "../../src/components/timeline/routeVisuals";
import { groundingStatusLabelForValue } from "../../src/components/timeline/timelineLabels";
import { AgentSession, ItineraryPlan, PlannerSegment } from "../../src/services/apiClient";
import { plannerStore } from "../../src/state/plannerStore";

const timelineStyles = readFileSync(resolve(process.cwd(), "src/styles.css"), "utf8");

beforeEach(() => {
  resetPlannerStore();
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  resetPlannerStore();
});

test("grounding status labels cover candidate-first POI states", () => {
  expect(groundingStatusLabelForValue("verified_amap")).toBe("已确认高德地点");
  expect(groundingStatusLabelForValue("provisional")).toBe("地点已匹配，夜间适配待核验");
  expect(groundingStatusLabelForValue("unresolved")).toBe("地点待补全");
  expect(groundingStatusLabelForValue("agent_selected_candidate")).toBe("Agent 已选高德候选");
  expect(groundingStatusLabelForValue("user_confirmed")).toBe("用户已确认高德地点");
  expect(groundingStatusLabelForValue("area_unresolved")).toBe("区域意图待细化");
  expect(groundingStatusLabelForValue("waiting_for_poi_grounding")).toBe("地点待补全");
  expect(groundingStatusLabelForValue("provider_rate_limited")).toBe("高德限流，稍后重试");
});

test("partial itinerary renders confirmed places and explicit empty timeline slots", () => {
  const base = planFixture();
  const plan: ItineraryPlan = {
    ...base,
    status: "partial",
    days: base.days.map((day, index) =>
      index === 0
        ? {
            ...day,
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
              label: "待补：街区漫步",
              timingStatus: "awaiting_route_confirmation",
              timingBasis: "planning_slot_window",
              constraintSummary: "按当前日程空闲区间安排；当前约束 14:00–16:00",
              placementAfterSegmentId: "seg_1",
              placementBeforeSegmentId: "seg_2"
            }]
          }
        : day
    )
  };

  render(
    <DailyTimeline
      plan={plan}
      selectedDayNumber={1}
      selectedSegmentId={null}
      onSelectSegment={() => undefined}
      onTransportChange={() => undefined}
    />
  );

  expect(screen.getByText("故宫博物院")).toBeTruthy();
  expect(screen.getByTestId("timeline-pending-slot").textContent).toContain("14:00-16:00");
  expect(screen.getByTestId("timeline-pending-slot").textContent).toContain("待补：街区漫步");
  expect(screen.getByTestId("timeline-pending-slot").getAttribute("style")).toContain("order: 840");
  expect(screen.getByText("按当前日程空闲区间安排；当前约束 14:00–16:00")).toBeTruthy();
  expect(screen.getByText("当前位置按已确认日程与时间约束预留，交通时间待路线确认。")).toBeTruthy();
  expect(screen.getByText("点击可在地图对比该槽位候选；最终地点仍在对话栏确认。")).toBeTruthy();
});

test("comparison overview timeline is read-only and exposes no writer or provider controls", () => {
  render(
    <DailyTimeline
      plan={planFixture()}
      readOnly
      selectedDayNumber={1}
      selectedSegmentId={null}
      onSelectSegment={() => undefined}
    />
  );

  expect(screen.queryByRole("button", { name: "优化路线" })).toBeNull();
  expect(screen.queryByRole("button", { name: "自动排期" })).toBeNull();
  expect(screen.queryByRole("button", { name: /编辑 .* 到达时间/ })).toBeNull();
  expect(screen.queryByRole("button", { name: /删除 / })).toBeNull();
  expect(screen.queryByRole("button", { name: /添加景点\/活动/ })).toBeNull();
  expect(screen.queryByRole("button", { name: /增加日程安排/ })).toBeNull();
  expect(screen.getByText("故宫博物院")).toBeTruthy();
  const timePill = screen.getByText("09:30").closest(".segment-time-pill") as HTMLElement;
  expect(timePill).toBeTruthy();
  expect(timePill.style.gridColumn).toBe("2");
  expect(timePill.style.gridRow).toBe("1 / 5");
  expect(timePill.style.zIndex).toBe("1");
  expect(timePill.closest(".segment-item")?.getAttribute("data-route-track-layer")).toBe("behind-content");
});

test("expanded visit facts span the timeline content columns and keep evidence readable", () => {
  const base = planFixture();
  const plan: ItineraryPlan = {
    ...base,
    visitFactsBySegment: {
      seg_1: {
        segmentId: "seg_1",
        amapPoiId: "B000A8UIN8",
        visitDate: "2026-10-01",
        refreshStatus: "completed",
        facts: {
          openingHours: {
            status: "verified",
            valueText: "08:30-16:00，需按预约时段入场",
            sourceRefs: [{ sourceName: "官方预约平台", url: "https://example.com/visit" }],
            queriedAt: "2026-09-03T01:00:00Z",
            expiresAt: "2026-09-04T01:00:00Z"
          },
          reservation: {
            status: "advisory",
            valueText: "建议提前预约并携带有效身份证件",
            sourceRefs: [],
            queriedAt: "2026-09-03T01:00:00Z",
            expiresAt: "2026-09-04T01:00:00Z"
          }
        },
        sourceRefs: [],
        evidenceFingerprint: "visit-facts-fixture",
        queriedAt: "2026-09-03T01:00:00Z",
        expiresAt: "2026-09-04T01:00:00Z"
      }
    }
  };

  render(
    <DailyTimeline
      plan={plan}
      selectedDayNumber={1}
      selectedSegmentId={null}
      onSelectSegment={() => undefined}
    />
  );

  const summary = screen.getByText("到访信息 · completed");
  const panel = summary.closest("details") as HTMLDetailsElement;
  expect(panel).toBeTruthy();
  expect(panel.style.gridColumn).toBe("2 / -1");
  expect(panel.style.minWidth).toBe("0");
  expect(panel.style.width).toBe("100%");

  fireEvent.click(summary);
  expect(panel.open).toBe(true);
  expect(screen.getByText("08:30-16:00，需按预约时段入场")).toBeTruthy();
  expect(screen.getByText("建议提前预约并携带有效身份证件")).toBeTruthy();
  expect(screen.getByRole("link", { name: "官方预约平台" }).getAttribute("href")).toBe(
    "https://example.com/visit"
  );

  expect(timelineStyles).toMatch(
    /\.segment-visit-facts dl > div\s*\{[^}]*display:\s*grid;[^}]*grid-template-columns:\s*minmax\(64px, auto\) minmax\(0, 1fr\);[^}]*min-width:\s*0;/s
  );
  expect(timelineStyles).toMatch(
    /\.segment-visit-facts dd\s*\{[^}]*display:\s*grid;[^}]*min-width:\s*0;[^}]*margin:\s*0;/s
  );
  expect(timelineStyles).toMatch(
    /\.segment-visit-facts dd span,[\s\S]*?\.segment-visit-facts a\s*\{[^}]*overflow-wrap:\s*anywhere;[^}]*white-space:\s*normal;/
  );
});

test("time-pending slot stays on its day without inventing a clock or sorting as midnight", () => {
  const base = planFixture();
  const plan: ItineraryPlan = {
    ...base,
    status: "partial",
    days: base.days.map((day, index) =>
      index === 0
        ? {
            ...day,
            pendingSlots: [{
              id: "pending_unknown",
              planningSlotId: "unknown-slot",
              briefId: "local",
              poolId: "unknown-pool",
              dayNumber: 1,
              rawNeed: "街区漫步",
              intentType: "neighborhood_walk",
              kind: "activity",
              state: "pending",
              label: "待补：街区漫步",
              timingStatus: "time_pending",
              placementBeforeSegmentId: "seg_2"
            }]
          }
        : day
    )
  };

  render(
    <DailyTimeline
      plan={plan}
      selectedDayNumber={1}
      selectedSegmentId={null}
      onSelectSegment={() => undefined}
    />
  );

  const pending = screen.getByTestId("timeline-pending-slot");
  expect(pending.textContent).toContain("时间待定");
  expect(pending.textContent).not.toMatch(/09:00|12:00|14:00|18:00|20:00/);
  expect(pending.getAttribute("style")).not.toContain("order: 1440");
});

test("waiting route options are not displayed as zero-distance real routes", () => {
  const plan = {
    ...planFixture(),
    routeOptions: planFixture().routeOptions.map((route) =>
      route.fromSegmentId === "seg_1" && route.toSegmentId === "seg_2"
        ? {
            ...route,
            distanceMeters: 0,
            durationSeconds: 0,
            durationMinutes: 0,
            polyline: [],
            routeStatus: "waiting_for_poi_grounding"
          }
        : route
    )
  };
  render(
    <DailyTimeline
      plan={plan}
      selectedDayNumber={1}
      selectedSegmentId="seg_1"
      onSelectSegment={() => undefined}
      onTransportChange={() => undefined}
    />
  );

  expect(screen.getByText("路线待地点确认后生成。")).toBeTruthy();
  expect(screen.queryByText(/0\.0 km/)).toBeNull();
  expect(screen.queryByText("0 分钟")).toBeNull();
  expect(screen.queryByText("0 分钟")).toBeNull();
});

test("timeline does not fall back to stale POI-id routes after segment replacement", () => {
  const plan = {
    ...planFixture(),
    routeOptions: planFixture().routeOptions.map((route) =>
      route.id === "route_1"
        ? {
            ...route,
            fromSegmentId: "seg_old_from",
            toSegmentId: "seg_old_to"
          }
        : route
    )
  };

  render(
    <DailyTimeline
      plan={plan}
      selectedDayNumber={1}
      selectedSegmentId="seg_1"
      onSelectSegment={() => undefined}
      onTransportChange={() => undefined}
    />
  );

  expect(screen.getByText("路线待地点确认后生成。")).toBeTruthy();
  expect(screen.queryByText(/公交\/地铁/)).toBeNull();
});

test("risk panel summarizes web search provider diagnostics", () => {
  const plan = {
    ...planFixture(),
    poiRiskAlerts: [
      {
        id: "risk_provider_diag",
        planId: "plan_workspace",
        segmentId: "seg_1",
        poiName: "故宫博物院",
        status: "unavailable",
        summary: "未完成近期公开信息搜索，景点风险判断不完整。",
        sourceName: "搜索结果",
        sourceUrl: null,
        sources: [
          {
            type: "riskSearchDiagnostics",
            query: "故宫博物院 2026 国庆 官方公告 预约 限流",
            riskStatusReason: "search_provider_unavailable",
            acceptedSourceCount: 0,
            sourceCount: 0,
            officialSourceCount: 0,
            rejectedStaleSourceCount: 0
          },
          {
            type: "webSearchProviderDiagnostics",
            providerName: "chained-web-search",
            attemptedProviders: [],
            successfulProviders: [],
            failedProviders: [],
            skippedProviders: ["tavily", "brave-web-search", "searxng"],
            providerDiagnostics: [
              { providerName: "tavily", status: "skipped", reason: "skipped_missing_config", resultCount: 0 },
              { providerName: "brave-web-search", status: "skipped", reason: "skipped_missing_config", resultCount: 0 },
              { providerName: "searxng", status: "skipped", reason: "skipped_missing_config", resultCount: 0 }
            ]
          }
        ],
        confidence: 0,
        failureReason: "search_provider_unavailable",
        userVisibleCaveat: "搜索供应商未配置，当前无法自动核验官方公告。",
        queriedAt: "2026-06-10T10:00:00Z"
      },
      {
        id: "risk_provider_success",
        planId: "plan_workspace",
        segmentId: "seg_2",
        poiName: "景山公园",
        status: "degraded",
        summary: "景山公园需要核对预约公告。",
        sourceName: "搜索结果",
        sourceUrl: "https://www.example.gov.cn/jingshan",
        sources: [
          {
            title: "景山公园官方预约公告",
            url: "https://www.example.gov.cn/jingshan",
            snippet: "2026年国庆预约公告。",
            credibilityRank: "official"
          },
          {
            type: "riskSearchDiagnostics",
            query: "景山公园 2026 国庆 官方公告 预约 限流",
            riskStatusReason: "search_success_degraded",
            acceptedSourceCount: 1,
            sourceCount: 2,
            officialSourceCount: 1,
            rejectedStaleSourceCount: 1
          },
          {
            type: "webSearchProviderDiagnostics",
            providerName: "chained-web-search",
            attemptedProviders: ["tavily", "brave-web-search"],
            successfulProviders: ["brave-web-search"],
            failedProviders: ["tavily"],
            skippedProviders: [],
            providerDiagnostics: [
              { providerName: "tavily", status: "failed", reason: "timeout", resultCount: 0 },
              { providerName: "brave-web-search", status: "success", reason: "ok", resultCount: 1 }
            ]
          }
        ],
        confidence: 0.62,
        failureReason: "search_success_degraded",
        userVisibleCaveat: "部分搜索供应商失败或跳过，当前风险判断仍需核对官方公告。",
        queriedAt: "2026-06-10T10:00:00Z"
      }
    ]
  };

  render(
    <DailyTimeline
      plan={plan}
      selectedDayNumber={1}
      selectedSegmentId="seg_1"
      onSelectSegment={() => undefined}
      onTransportChange={() => undefined}
    />
  );

  fireEvent.click(screen.getByRole("button", { name: "展开风险" }));
  expect(screen.getAllByText(/搜索供应商未配置，当前无法自动核验官方公告。/).length).toBeGreaterThan(0);
  expect(screen.getByText(/Tavily 超时；Brave 成功 1 条/)).toBeTruthy();
  expect(screen.getAllByText("风险待核验").length).toBeGreaterThan(0);
  expect(screen.getByText("来源统计：已采纳来源 0 条；官方来源 0 条；已拒绝过期/低相关 0 条")).toBeTruthy();
  expect(screen.getByText("来源统计：已采纳来源 1 条；官方来源 1 条；已拒绝过期/低相关 1 条（过期 1 条）")).toBeTruthy();
  expect(screen.queryByText("2026年国庆预约公告。")).toBeNull();
  const riskArea = screen.getByLabelText("POI risk search alerts");
  const jingshanRisk = within(riskArea).getByText("景山公园").closest("details") as HTMLDetailsElement;
  fireEvent.click(within(riskArea).getByText("景山公园"));
  fireEvent.click(within(jingshanRisk).getByText("Debug 详情"));
  expect(screen.getByText("2026年国庆预约公告。")).toBeTruthy();
});

test("timeline maps route legs across non-route meal segments", () => {
  const base = planFixture();
  const mealSegment: PlannerSegment = {
    id: "seg_lunch",
    startTime: "12:00",
    endTime: "13:00",
    kind: "meal",
    poi: {
      id: "poi_lunch",
      name: "午餐",
      city: "北京",
      category: "meal",
      latitude: null,
      longitude: null,
      source: "agent-text-timeline",
      confidence: 0.35,
      type: "餐饮时间",
      district: "",
      address: "",
      sourceNote: "普通用餐时间；可选择顺路餐厅。mealGrounding=optional_waiting; requiredGrounding=false; pendingMeal=true；groundingStatus：optional_waiting；intentType：meal；routeAnchor=false；needsConcretePoi=true。",
      groundingStatus: "optional_waiting",
      mapReady: false,
      routeable: false,
      needsConcretePoi: true
    },
    transportMode: "public_transit",
    estimatedCost: 0,
    notes: "普通用餐时间；可选择顺路餐厅，不强制餐厅 grounding。"
  };
  const plan = {
    ...base,
    days: [
      {
        ...base.days[0],
        segments: [base.days[0].segments[0], mealSegment, base.days[0].segments[1]]
      }
    ]
  };

  render(
    <DailyTimeline
      plan={plan}
      selectedDayNumber={1}
      selectedSegmentId="seg_1"
      onSelectSegment={() => undefined}
      onTransportChange={() => undefined}
    />
  );

  expect(screen.queryByText(/路线待地点确认后生成/)).toBeNull();
  expect(screen.getAllByText(/已知路线 1\.9 km · 1\/1 路段已生成/).length).toBeGreaterThan(0);
  expect(screen.getByText("普通用餐时间；可选择顺路餐厅，不强制餐厅 grounding。")).toBeTruthy();

  cleanup();
  render(
    <DailyTimeline
      plan={plan}
      selectedDayNumber={1}
      selectedSegmentId="seg_lunch"
      onSelectSegment={() => undefined}
      onTransportChange={() => undefined}
    />
  );

  expect(screen.queryByText("路线待地点确认后生成。")).toBeNull();
  expect(screen.getAllByText(/已知路线 1\.9 km · 1\/1 路段已生成/).length).toBeGreaterThan(0);
});

test("visit and activity placeholders that need concrete POI are not route anchors", () => {
  expect(
    isRouteAnchorSegment({
      kind: "activity",
      notes: "groundingStatus：waiting_for_poi_grounding；needsConcretePoi=true；routeAnchor=true",
      poi: {
        routeable: true,
        needsConcretePoi: true,
        groundingStatus: "waiting_for_poi_grounding"
      }
    })
  ).toBe(false);
  expect(
    isRouteAnchorSegment({
      kind: "visit",
      poi: {
        routeable: true,
        sourceNote: "groundingStatus：provider_rate_limited；needsConcretePoi=true"
      }
    })
  ).toBe(false);
  expect(
    isRouteAnchorSegment({
      kind: "visit",
      poi: {
        routeable: true,
        needsConcretePoi: false,
        groundingStatus: "verified_amap"
      }
    })
  ).toBe(true);
});

test("timeline treats a concrete meal POI as a route anchor", () => {
  const base = planFixture();
  const mealSegment: PlannerSegment = {
    id: "seg_lunch",
    startTime: "12:00",
    endTime: "13:00",
    kind: "meal",
    poi: {
      id: "poi_lunch",
      name: "麦当劳(清华大学店)",
      city: "北京",
      category: "food",
      latitude: 39.995,
      longitude: 116.332,
      source: "amap-place-search",
      confidence: 0.91,
      amapId: "B000MEAL",
      type: "餐饮服务;快餐厅",
      district: "海淀区",
      address: "清华大学附近",
      sourceNote: "高德 WebService POI 搜索，限定当前城市，extensions=all",
      groundingStatus: "verified_amap",
      mapReady: true,
      routeable: true,
      needsConcretePoi: false
    },
    transportMode: "public_transit",
    estimatedCost: 35,
    notes: "已替换为具体餐厅，应参与上下路线。"
  };
  const routeToMeal = {
    ...base.routeOptions[0],
    id: "route_lunch_in",
    fromSegmentId: "seg_1",
    toSegmentId: "seg_lunch",
    fromPoiId: "poi_1",
    toPoiId: "poi_lunch",
    distanceMeters: 900,
    durationSeconds: 600,
    durationMinutes: 10,
    polyline: [[116.3972, 39.9163], [116.332, 39.995]]
  };
  const routeFromMeal = {
    ...base.routeOptions[0],
    id: "route_lunch_out",
    fromSegmentId: "seg_lunch",
    toSegmentId: "seg_2",
    fromPoiId: "poi_lunch",
    toPoiId: "poi_2",
    distanceMeters: 1100,
    durationSeconds: 720,
    durationMinutes: 12,
    polyline: [[116.332, 39.995], [116.3969, 39.9236]]
  };
  const plan = {
    ...base,
    days: [
      {
        ...base.days[0],
        segments: [base.days[0].segments[0], mealSegment, base.days[0].segments[1]]
      }
    ],
    routeOptions: [routeToMeal, routeFromMeal]
  };

  const { container } = render(
    <DailyTimeline
      plan={plan}
      selectedDayNumber={1}
      selectedSegmentId="seg_1"
      onSelectSegment={() => undefined}
      onTransportChange={() => undefined}
    />
  );

  expect(container.querySelectorAll(".route-leg-summary")).toHaveLength(2);
  expect(screen.queryByText("路线待地点确认后生成。")).toBeNull();
  expect(screen.getByText("麦当劳(清华大学店)")).toBeTruthy();
});

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
    preferenceCard: null,
    preferenceMemory: null,
    pendingPoiCandidates: [],
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
  });
}

test("switches right-panel tabs and shows pending states", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => jsonResponse({ mode: "mock", default: [], mock: [] }))
  );

  render(<AppShell />);

  fireEvent.click(screen.getByText("费用明细"));
  expect(screen.getByText("行程生成后会展示餐饮、交通、票务、住宿和其它预算。")).toBeTruthy();

  fireEvent.click(screen.getByText("行程对比"));
  expect(screen.getByText("行程对比待接入")).toBeTruthy();
  expect(screen.getByText("生成多个对比方案后将在这里展示各方案的差异和决策依据。")).toBeTruthy();

  fireEvent.click(screen.getByText("行程总览"));
  expect(screen.getByText("行程时间轴待生成。")).toBeTruthy();
});

test("initializes from current persisted agent session and restores timeline", async () => {
  const plan = {
    ...planFixture(),
    weatherSignals: [
      {
        ...planFixture().weatherSignals[0],
        source: "高德天气",
        providerName: "amap-weather-provider",
        dataStatus: "available",
        confidence: 0.91,
        userVisibleCaveat: "天气风险提示仅供参考，出行前请重新查询。"
      }
    ],
    trafficCrowdingSignals: [
      {
        ...planFixture().trafficCrowdingSignals[0],
        source: "amap-traffic",
        realDataAvailable: false
      }
    ],
    poiRiskAlerts: [
      {
        id: "risk_copy_1",
        planId: "plan_workspace",
        segmentId: "seg_1",
        poiName: "故宫博物院",
        status: "available",
        summary: "故宫博物院暑期参观需要提前预约，部分入口施工绕行。",
        sourceName: "故宫博物院预约公告",
        sourceUrl: "https://example.com/palace-notice",
        sources: [
          {
            title: "故宫博物院预约公告",
            url: "https://example.com/palace-notice",
            snippet: "故宫博物院暑期参观需要提前预约。"
          }
        ],
        confidence: 0.72,
        failureReason: null,
        userVisibleCaveat: "风险提醒基于公开搜索结果，仍需以景区官方公告和现场管理为准。",
        queriedAt: "2026-06-10T10:00:00Z"
      },
      {
        id: "risk_copy_2",
        planId: "plan_workspace",
        segmentId: "seg_2",
        poiName: "景山公园",
        status: "degraded",
        summary: "景山公园周边周末人流较大，建议错峰。",
        sourceName: "景山公园客流提示",
        sourceUrl: "https://example.com/jingshan",
        sources: [
          {
            title: "景山公园客流提示",
            url: "https://example.com/jingshan",
            snippet: "周末人流较大。"
          },
          {
            type: "riskSearchDiagnostics",
            query: "景山公园 2026 国庆 官方公告 预约 限流",
            riskStatusReason: "search_success_degraded",
            acceptedSourceCount: 1,
            sourceCount: 2,
            officialSourceCount: 1,
            rejectedStaleSourceCount: 1
          },
          {
            type: "webSearchProviderDiagnostics",
            providerName: "chained-web-search",
            attemptedProviders: ["tavily", "brave-web-search"],
            successfulProviders: ["brave-web-search"],
            failedProviders: ["tavily"],
            skippedProviders: [],
            providerDiagnostics: [
              { providerName: "tavily", status: "failed", reason: "timeout", resultCount: 0 },
              { providerName: "brave-web-search", status: "success", reason: "ok", resultCount: 1 }
            ]
          }
        ],
        confidence: 0.6,
        failureReason: "Agent 风险判断服务暂不可用，已保留公开搜索摘要。",
        userVisibleCaveat: "风险判断不完整，请核对来源。",
        queriedAt: "2026-06-10T10:00:00Z"
      }
    ]
  };
  const writeText = mockClipboard();
  const fetchMock = vi.fn(async (url: RequestInfo | URL, _init?: RequestInit) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) {
      return jsonResponse({ mode: "mock", default: [], mock: [] });
    }
    if (path.endsWith("/preferences/memory")) {
      return jsonResponse({
        userId: "default",
        memoryText: "# 我的旅行偏好\n\n## 旅行节奏\n- 喜欢轻松不赶路。\n",
        autoUpdateEnabled: true,
        createdAt: "2026-06-10T10:00:00Z",
        updatedAt: "2026-06-10T10:00:00Z"
      });
    }
    if (path.endsWith("/agent/sessions/current")) {
      return jsonResponse({
        sessionId: "sess_restored",
        status: "active",
        city: "北京",
        title: "北京会话",
        activePlanId: plan.id,
        activeVersionId: "ver_restored",
        turns: [
          {
            id: "turn_restored_user",
            role: "user",
            content: "北京一天轻松一点",
            turnIndex: 1,
            status: "active",
            createdAt: "2026-06-10T10:00:00Z",
            updatedAt: "2026-06-10T10:00:00Z"
          },
          {
            id: "turn_restored_assistant",
            role: "assistant",
            content: "已恢复服务端保存的行程。",
            turnIndex: 2,
            status: "active",
            itineraryVersionId: "ver_restored",
            planningSteps: [
              {
                type: "write",
                label: "写入 SQLite version / patch",
                status: "completed",
                detail: "已保存新的 itinerary version / patch。",
                providerName: "sqlite",
                fallbackUsed: false,
                timestamp: "2026-06-10T10:00:00Z"
              }
            ],
            toolEvents: [
              {
                type: "tool",
                label: "联网搜索票务/预约",
                status: "failed",
                detail: "博查联网搜索失败，未使用 mock 数据。",
                providerName: "bocha-web-search",
                fallbackUsed: false,
                failureReason: "WEB_SEARCH_API_KEY is not configured.",
                timestamp: "2026-06-10T10:00:00Z"
              }
            ],
            createdAt: "2026-06-10T10:00:01Z",
            updatedAt: "2026-06-10T10:00:01Z"
          }
        ],
        itinerary: plan,
        pendingPoiCandidates: []
      });
    }
    if (path.endsWith("/agent/sessions")) {
      return jsonResponse({ detail: "should not create session on init" }, 500);
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);

  await waitFor(() => expect(screen.getAllByText("北京地图行程草案").length).toBeGreaterThan(0));
  expect(screen.getByText("北京一天轻松一点")).toBeTruthy();
  expect(screen.getByText("已恢复服务端保存的行程。")).toBeTruthy();
  await waitFor(() => expect(screen.getByText("历史中轴线与老北京风情")).toBeTruthy());
  const costsTab = screen.getByRole("tab", { name: "费用明细" });
  const copyTimelineButton = screen.getByRole("button", { name: "复制完整 Agent 调试上下文" });
  expect(costsTab).toBeTruthy();
  await waitFor(() =>
    expect(plannerStore.getSnapshot().timelineCopyText).toContain("时间轴：北京地图行程草案")
  );
  fireEvent.click(copyTimelineButton);
  await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
  const copiedTimeline = String(writeText.mock.calls[0][0]);
  expect(copiedTimeline).toContain("时间轴：北京地图行程草案");
  expect(copiedTimeline).toContain("Day 1：历史中轴线与老北京风情");
  expect(copiedTimeline).toContain("09:30 故宫博物院");
  expect(copiedTimeline).toContain("下一段路线：公交/地铁");
  expect(copiedTimeline).toContain("风险提示：");
  expect(copiedTimeline).toContain("天气：");
  expect(copiedTimeline).toContain("摘要：晴，适合拍照");
  expect(copiedTimeline).toContain("影响：户外拍照条件较好");
  expect(copiedTimeline).toContain("来源：高德天气");
  expect(copiedTimeline).toContain("查询 2026/6/4 08:00:00");
  expect(copiedTimeline).toContain("置信度 91%");
  expect(copiedTimeline).toContain("天气风险提示仅供参考，出行前请重新查询。");
  expect(copiedTimeline).toContain("拥挤：");
  expect(copiedTimeline).toContain("依据：工作日早高峰估算");
  expect(copiedTimeline).toContain("来源：amap-traffic");
  expect(copiedTimeline).toContain("查询 2026/6/4 08:00:00");
  expect(copiedTimeline).toContain("故宫博物院：已查询");
  expect(copiedTimeline).toContain("故宫博物院暑期参观需要提前预约，部分入口施工绕行。");
  expect(copiedTimeline).toContain("https://example.com/palace-notice");
  expect(copiedTimeline).toContain("风险提醒基于公开搜索结果，仍需以景区官方公告和现场管理为准。");
  expect(copiedTimeline).toContain("景山公园：部分数据可用");
  expect(copiedTimeline).toContain("风险判断提示：Agent 风险判断服务暂不可用，已保留公开搜索摘要。");
  expect(copiedTimeline).not.toContain("Debug 详情：");
  expect(copiedTimeline).toContain("riskSearchDiagnostics");
  expect(copiedTimeline).toContain("webSearchProviderDiagnostics");
  fireEvent.click(screen.getByRole("button", { name: "北京地图行程草案" }));
  fireEvent.change(screen.getByLabelText("旅行标题输入"), { target: { value: "屏幕上未保存的新标题" } });
  fireEvent.click(copyTimelineButton);
  await waitFor(() => expect(writeText).toHaveBeenCalledTimes(2));
  const copiedDraftTimeline = String(writeText.mock.calls[1][0]);
  expect(copiedDraftTimeline).toContain("时间轴：屏幕上未保存的新标题");
  expect(screen.queryByLabelText("Agent planning process")).toBeNull();
  const turnPanel = screen.getByLabelText("Agent turn planning process");
  expect(within(turnPanel).getByText("查看详细执行记录")).toBeTruthy();
  expect(within(turnPanel).getByText("写入 SQLite version / patch")).toBeTruthy();
  expect(within(turnPanel).getByText("联网搜索票务/预约")).toBeTruthy();
  fireEvent.click(within(turnPanel).getByText("查看失败详情"));
  expect(within(turnPanel).getAllByText(/相关服务暂时不可用/).length).toBeGreaterThan(0);
  expect(fetchMock.mock.calls.some((call) => String(call[0]).endsWith("/agent/sessions") && call[1]?.method === "POST")).toBe(false);
});

test("lists, switches, creates, and deletes persisted agent sessions", async () => {
  const beijingPlan = planFixture();
  const shanghaiPlan = {
    ...planFixture(),
    id: "plan_shanghai",
    title: "上海地图行程草案",
    city: "上海",
    days: planFixture().days.map((day) => ({
      ...day,
      title: "外滩与街区随拍",
      segments: day.segments.map((segment) => ({
        ...segment,
        poi: { ...segment.poi, city: "上海" }
      }))
    }))
  };
  const newPlan = {
    ...planFixture(),
    id: "plan_new",
    title: "北京新对话行程草案"
  };
  const sessionA = agentSessionFixture("sess_beijing", "北京历史会话", beijingPlan, "北京一天轻松一点", "ver_beijing");
  const sessionB = agentSessionFixture("sess_shanghai", "上海历史会话", shanghaiPlan, "上海一天随拍", "ver_shanghai");
  const sessionC = agentSessionFixture("sess_new", "北京 AI 行程", newPlan, "新建空对话", "ver_new");
  let sessionSummaries = [agentSessionSummary(sessionA), agentSessionSummary(sessionB)];
  let createSessionCount = 0;

  const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) {
      return jsonResponse({ mode: "mock", default: [], mock: [] });
    }
    if (path.endsWith("/preferences/memory")) {
      return jsonResponse({
        userId: "default",
        memoryText: "",
        autoUpdateEnabled: true,
        createdAt: "2026-06-10T10:00:00Z",
        updatedAt: "2026-06-10T10:00:00Z"
      });
    }
    if (path.endsWith("/agent/sessions/current")) {
      return jsonResponse(sessionA);
    }
    if (path.endsWith("/agent/sessions") && !init?.method) {
      return jsonResponse({ sessions: sessionSummaries });
    }
    if (path.endsWith("/agent/sessions") && init?.method === "POST") {
      createSessionCount += 1;
      sessionSummaries = [agentSessionSummary(sessionC), ...sessionSummaries];
      return jsonResponse(sessionC);
    }
    if (path.endsWith("/agent/sessions/sess_beijing")) {
      return jsonResponse(sessionA);
    }
    if (path.endsWith("/agent/sessions/sess_shanghai")) {
      return jsonResponse(sessionB);
    }
    if (path.endsWith("/agent/sessions/sess_new") && init?.method === "DELETE") {
      sessionSummaries = [agentSessionSummary(sessionB), agentSessionSummary(sessionA)];
      return jsonResponse({ sessions: sessionSummaries });
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);

  await waitFor(() => expect(screen.getAllByText("北京地图行程草案").length).toBeGreaterThan(0));
  const selector = screen.getByLabelText("选择对话") as HTMLSelectElement;
  await waitFor(() => expect(within(selector).getByText("上海历史会话 · 2 轮")).toBeTruthy());

  fireEvent.change(selector, { target: { value: "sess_shanghai" } });
  await waitFor(() => expect(screen.getAllByText("上海地图行程草案").length).toBeGreaterThan(0));
  expect(screen.getByText("上海一天随拍")).toBeTruthy();
  expect(plannerStore.getSnapshot().activeVersionId).toBe("ver_shanghai");

  fireEvent.click(screen.getByText("新建对话"));
  await waitFor(() => expect(screen.getAllByText("北京新对话行程草案").length).toBeGreaterThan(0));
  expect(plannerStore.getSnapshot().agentSession?.sessionId).toBe("sess_new");
  expect(fetchMock.mock.calls.some((call) => String(call[0]).endsWith("/agent/sessions") && call[1]?.method === "POST")).toBe(true);

  fireEvent.click(screen.getByText("删除对话及行程"));
  await waitFor(() => expect(plannerStore.getSnapshot().agentSession).toBeNull());
  expect(plannerStore.getSnapshot().conversationTurns).toHaveLength(0);
  expect(plannerStore.getSnapshot().activeVersionId).toBeNull();
  expect(plannerStore.getSnapshot().itineraryPlan).toBeNull();
  expect(fetchMock.mock.calls.some((call) => String(call[0]).endsWith("/agent/sessions/sess_new") && call[1]?.method === "DELETE")).toBe(true);
  expect(fetchMock.mock.calls.filter((call) => String(call[0]).endsWith("/agent/sessions") && call[1]?.method === "POST")).toHaveLength(1);
});

test("keeps the workspace empty after deleting the last session", async () => {
  plannerStore.setState({
    selectedCity: "北京",
    agentSession: null,
    agentSessions: [],
    conversationTurns: [],
    activeVersionId: null,
    itineraryPlan: null,
    pendingPoiCandidates: []
  });
  const session = agentSessionFixture("sess_delete_fail_create", "北京待删会话", planFixture(), "北京一天", "ver_delete");
  const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) {
      return jsonResponse({ mode: "mock", default: [], mock: [] });
    }
    if (path.endsWith("/preferences/memory")) {
      return jsonResponse({
        userId: "default",
        memoryText: "",
        autoUpdateEnabled: true,
        createdAt: "2026-06-10T10:00:00Z",
        updatedAt: "2026-06-10T10:00:00Z"
      });
    }
    if (path.endsWith("/agent/sessions/current")) {
      return jsonResponse(session);
    }
    if (path.endsWith("/agent/sessions") && !init?.method) {
      return jsonResponse({ sessions: [agentSessionSummary(session)] });
    }
    if (path.endsWith("/agent/sessions/sess_delete_fail_create") && init?.method === "DELETE") {
      return jsonResponse({ sessions: [] });
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);

  await waitFor(() => expect(plannerStore.getSnapshot().agentSession?.sessionId).toBe("sess_delete_fail_create"));
  fireEvent.click(screen.getByText("删除对话及行程"));

  await waitFor(() => expect(plannerStore.getSnapshot().agentSession).toBeNull());
  expect(plannerStore.getSnapshot().conversationTurns).toHaveLength(0);
  expect(plannerStore.getSnapshot().activeVersionId).toBeNull();
  expect(plannerStore.getSnapshot().itineraryPlan).toBeNull();
  expect(screen.queryByText("北京地图行程草案")).toBeNull();
  expect(fetchMock.mock.calls.some((call) => String(call[0]).endsWith("/agent/sessions") && call[1]?.method === "POST")).toBe(false);
});

test("keeps the current session and timeline when hard delete fails", async () => {
  const session = agentSessionFixture(
    "sess_delete_blocked",
    "北京运行中会话",
    planFixture(),
    "北京一天",
    "ver_delete_blocked"
  );
  const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) {
      return jsonResponse({ mode: "mock", default: [], mock: [] });
    }
    if (path.endsWith("/preferences/memory")) {
      return jsonResponse({ userId: "default", memoryText: "", autoUpdateEnabled: true });
    }
    if (path.endsWith("/agent/sessions/current")) {
      return jsonResponse(session);
    }
    if (path.endsWith("/agent/sessions") && !init?.method) {
      return jsonResponse({ sessions: [agentSessionSummary(session)] });
    }
    if (path.endsWith("/agent/sessions/sess_delete_blocked") && init?.method === "DELETE") {
      return jsonResponse({ detail: "当前对话仍在执行，请稍后再删除。" }, 409);
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);
  await waitFor(() => expect(plannerStore.getSnapshot().agentSession?.sessionId).toBe("sess_delete_blocked"));
  fireEvent.click(screen.getByText("删除对话及行程"));

  await waitFor(() => expect(screen.getByText("当前对话仍在执行，请稍后再删除。")).toBeTruthy());
  expect(plannerStore.getSnapshot().agentSession?.sessionId).toBe("sess_delete_blocked");
  expect(plannerStore.getSnapshot().activeVersionId).toBe("ver_delete_blocked");
  expect(plannerStore.getSnapshot().itineraryPlan?.id).toBe(planFixture().id);
});

test("uses session-scoped preference memory when switching agent sessions", async () => {
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
  const sessionA = {
    ...agentSessionFixture("sess_a", "北京偏好会话", planFixture(), "北京一天", "ver_a"),
    preferenceMemory: preferenceMemoryFixture("sess_a", "# 我的旅行偏好\n\n## 旅行节奏\n- 北京会话偏好。\n")
  };
  const sessionB = {
    ...agentSessionFixture("sess_b", "上海偏好会话", { ...planFixture(), id: "plan_b", title: "上海地图行程草案", city: "上海" }, "上海一天", "ver_b"),
    preferenceMemory: preferenceMemoryFixture("sess_b", "# 我的旅行偏好\n\n## 旅行节奏\n- 上海会话偏好。\n")
  };
  const fetchMock = vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) {
      return jsonResponse({ mode: "mock", default: [], mock: [] });
    }
    if (path.endsWith("/agent/sessions/current")) {
      return jsonResponse(sessionA);
    }
    if (path.endsWith("/agent/sessions")) {
      return jsonResponse({ sessions: [agentSessionSummary(sessionA), agentSessionSummary(sessionB)] });
    }
    if (path.endsWith("/agent/sessions/sess_b")) {
      return jsonResponse(sessionB);
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);

  await waitFor(() => expect(plannerStore.getSnapshot().agentSession?.sessionId).toBe("sess_a"));
  expect(plannerStore.getSnapshot().preferenceMemory?.memoryText).toContain("北京会话偏好");

  fireEvent.change(screen.getByLabelText("选择对话"), { target: { value: "sess_b" } });

  await waitFor(() => expect(plannerStore.getSnapshot().agentSession?.sessionId).toBe("sess_b"));
  expect(plannerStore.getSnapshot().preferenceMemory?.sessionId).toBe("sess_b");
  expect(plannerStore.getSnapshot().preferenceMemory?.memoryText).toContain("上海会话偏好");
  expect(plannerStore.getSnapshot().preferenceMemory?.memoryText).not.toContain("北京会话偏好");
  expect(fetchMock.mock.calls.some((call) => String(call[0]).endsWith("/preferences/memory"))).toBe(false);
});

test("selecting timeline POIs clears stale map candidate selection", async () => {
  const plan = planFixture();
  const session = agentSessionFixture("sess_select_segments", "北京行程", plan, "北京一天", "ver_select");
  const fetchMock = vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) {
      return jsonResponse({ mode: "mock", default: [], mock: [] });
    }
    if (path.endsWith("/agent/sessions/current")) {
      return jsonResponse(session);
    }
    if (path.endsWith("/agent/sessions")) {
      return jsonResponse({ sessions: [agentSessionSummary(session)] });
    }
    if (path.endsWith("/map/config")) {
      return jsonResponse({ provider: "amap", enabled: false, jsApiKey: "" });
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);

  await waitFor(() => expect(plannerStore.getSnapshot().selectedSegmentId).toBe("seg_1"));
  plannerStore.setState({
    selectedMapPoi: amapPoiFixture(),
    candidateMapPois: [amapPoiFixture()]
  });

  const targetSegmentName = screen.getAllByTestId("timeline-segment-name").find((node) => node.textContent === "景山公园");
  expect(targetSegmentName).toBeTruthy();
  fireEvent.click(targetSegmentName!.closest("button") as HTMLButtonElement);

  await waitFor(() => expect(plannerStore.getSnapshot().selectedSegmentId).toBe("seg_2"));
  expect(plannerStore.getSnapshot().selectedMapPoi).toBeNull();
  expect(plannerStore.getSnapshot().selectedDayNumber).toBe(1);
});

test("ignores stale session switch responses and keeps the latest selected session", async () => {
  plannerStore.setState({
    selectedCity: "北京",
    agentSession: null,
    agentSessions: [],
    conversationTurns: [],
    activeVersionId: null,
    itineraryPlan: null,
    pendingPoiCandidates: [],
    selectedSegmentId: null,
    selectedRouteOptionId: null
  });
  const sessionA = agentSessionFixture("sess_beijing", "北京历史会话", planFixture(), "北京一天轻松一点", "ver_beijing");
  const sessionB = agentSessionFixture("sess_shanghai", "上海历史会话", { ...planFixture(), id: "plan_shanghai", title: "上海地图行程草案", city: "上海" }, "上海一天随拍", "ver_shanghai");
  const sessionC = agentSessionFixture("sess_guangzhou", "广州历史会话", { ...planFixture(), id: "plan_guangzhou", title: "广州地图行程草案", city: "广州" }, "广州一天街区", "ver_guangzhou");
  const slowShanghai = deferred<Response>();
  const fetchMock = vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) {
      return jsonResponse({ mode: "mock", default: [], mock: [] });
    }
    if (path.endsWith("/preferences/memory")) {
      return jsonResponse({ userId: "default", memoryText: "", autoUpdateEnabled: true, createdAt: "2026-06-10T10:00:00Z", updatedAt: "2026-06-10T10:00:00Z" });
    }
    if (path.endsWith("/agent/sessions/current")) {
      return jsonResponse(sessionA);
    }
    if (path.endsWith("/agent/sessions")) {
      return jsonResponse({ sessions: [agentSessionSummary(sessionA), agentSessionSummary(sessionB), agentSessionSummary(sessionC)] });
    }
    if (path.endsWith("/agent/sessions/sess_shanghai")) {
      return slowShanghai.promise;
    }
    if (path.endsWith("/agent/sessions/sess_guangzhou")) {
      return jsonResponse(sessionC);
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);

  await waitFor(() => expect(plannerStore.getSnapshot().agentSession?.sessionId).toBe("sess_beijing"));
  const selector = screen.getByLabelText("选择对话") as HTMLSelectElement;
  fireEvent.change(selector, { target: { value: "sess_shanghai" } });
  fireEvent.change(selector, { target: { value: "sess_guangzhou" } });

  await waitFor(() => expect(plannerStore.getSnapshot().agentSession?.sessionId).toBe("sess_guangzhou"));
  slowShanghai.resolve(jsonResponse(sessionB));
  await nextTick();

  expect(plannerStore.getSnapshot().agentSession?.sessionId).toBe("sess_guangzhou");
  expect(plannerStore.getSnapshot().activeVersionId).toBe("ver_guangzhou");
  expect(screen.getAllByText("广州地图行程草案").length).toBeGreaterThan(0);
});

test("new agent session does not inherit the previous session active version or timeline", async () => {
  plannerStore.setState({
    selectedCity: "北京",
    agentSession: null,
    agentSessions: [],
    conversationTurns: [],
    activeVersionId: null,
    itineraryPlan: null,
    pendingPoiCandidates: [],
    selectedSegmentId: null,
    selectedRouteOptionId: null
  });
  const sessionA = agentSessionFixture("sess_old", "旧北京会话", planFixture(), "北京一天轻松一点", "ver_old");
  const emptySession = {
    sessionId: "sess_empty",
    status: "active",
    city: "北京",
    title: "北京 AI 行程",
    activePlanId: "plan_empty",
    activeVersionId: null,
    turns: [],
    itinerary: null,
    pendingPoiCandidates: [],
    preferenceMemory: null
  };
  const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) {
      return jsonResponse({ mode: "mock", default: [], mock: [] });
    }
    if (path.endsWith("/preferences/memory")) {
      return jsonResponse({ userId: "default", memoryText: "", autoUpdateEnabled: true, createdAt: "2026-06-10T10:00:00Z", updatedAt: "2026-06-10T10:00:00Z" });
    }
    if (path.endsWith("/agent/sessions/current")) {
      return jsonResponse(sessionA);
    }
    if (path.endsWith("/agent/sessions") && !init?.method) {
      return jsonResponse({
        sessions: [
          agentSessionSummary(sessionA),
          {
            sessionId: "sess_empty",
            title: "北京 AI 行程",
            city: "北京",
            status: "active",
            activePlanId: "plan_empty",
            activeVersionId: null,
            turnCount: 0,
            createdAt: "2026-06-10T10:00:00Z",
            updatedAt: "2026-06-10T10:00:00Z"
          }
        ]
      });
    }
    if (path.endsWith("/agent/sessions") && init?.method === "POST") {
      return jsonResponse(emptySession);
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);

  await waitFor(() => expect(plannerStore.getSnapshot().activeVersionId).toBe("ver_old"));
  fireEvent.click(screen.getByText("新建对话"));

  await waitFor(() => expect(plannerStore.getSnapshot().agentSession?.sessionId).toBe("sess_empty"));
  expect(plannerStore.getSnapshot().activeVersionId).toBeNull();
  expect(plannerStore.getSnapshot().itineraryPlan).toBeNull();
});

test("edits trip title through patch API and validates empty or overlong titles", async () => {
  const fetchMock = mockPatchFetch((plan, operations) => ({
    ...plan,
    title: String(operations[0].value ?? plan.title)
  }));
  renderTimeline();

  fireEvent.click(screen.getByText("北京地图行程草案"));
  fireEvent.change(screen.getByLabelText("旅行标题输入"), { target: { value: "" } });
  fireEvent.click(screen.getByText("确认"));
  expect(screen.getByText("旅行标题不能为空。")).toBeTruthy();

  fireEvent.change(screen.getByLabelText("旅行标题输入"), { target: { value: "北京 2 日行程" } });
  fireEvent.click(screen.getByText("确认"));
  await waitFor(() => expect(screen.getByText("北京 2 日行程")).toBeTruthy());
  const requestBody = JSON.parse(String(fetchMock.mock.calls[0][1]?.body ?? "{}")) as { baseVersionId?: string };
  expect(fetchMock).toHaveBeenCalledWith(
    "http://localhost:8000/api/itineraries/plan_workspace/patch",
    expect.objectContaining({
      method: "POST",
      body: expect.stringContaining("replace_trip_title")
    })
  );
  expect(requestBody.baseVersionId).toBe("ver_workspace");

  fireEvent.click(screen.getByText("北京 2 日行程"));
  fireEvent.change(screen.getByLabelText("旅行标题输入"), {
    target: { value: "这是一个超过三十二个字的旅行标题用于验证基础校验不能通过并且应该被拒绝保存" }
  });
  fireEvent.click(screen.getByText("确认"));
  expect(screen.getByText("旅行标题不能超过 32 个字。")).toBeTruthy();
});

test("timeline renders POI grounding lifecycle without guessing from source", () => {
  const base = threeSegmentPlanFixture();
  const lifecyclePlan = {
    ...base,
    days: [
      {
        ...base.days[0],
        segments: base.days[0].segments.map((segment, index) => {
          if (index === 0) {
            return {
              ...segment,
              estimatedCost: 0,
              poi: {
                ...segment.poi,
                amapId: undefined,
                latitude: null,
                longitude: null,
                source: "agent-text-timeline",
                sourceNote: "Agent 先写入可编辑时间轴，占位 POI 未完成地图 grounding。",
                confidence: 0.35,
                groundingStatus: "draft_only",
                mapReady: false,
                routeable: false,
                grounding: {
                  status: "draft_only",
                  groundingStatus: "draft_only",
                  mapReady: false,
                  routeable: false,
                  hasProviderPoiId: false,
                  hasCoordinates: false,
                  needsVerification: true
                }
              }
            };
          }
          if (index === 1) {
            return {
              ...segment,
              poi: {
                ...segment.poi,
                amapId: "B000ANCHOR",
                source: "agent-text-timeline",
                sourceNote: "已匹配高德地图锚点，路线可用，仍需用户确认是否为目标地点。地图锚点：景山公园；高德 POI 待校验",
                confidence: 0.4,
                groundingStatus: "routeable_anchor",
                mapReady: true,
                routeable: true,
                matchedAmapName: "景山公园",
                grounding: {
                  status: "routeable_anchor",
                  groundingStatus: "routeable_anchor",
                  mapReady: true,
                  routeable: true,
                  matchedAmapName: "景山公园",
                  hasProviderPoiId: true,
                  hasCoordinates: true,
                  needsVerification: true
                }
              }
            };
          }
          return {
            ...segment,
            poi: {
              ...segment.poi,
              amapId: "B000VERIFIED",
              source: "amap-place-search",
              confidence: 0.91,
              groundingStatus: "verified_amap",
              mapReady: true,
              routeable: true,
              matchedAmapName: segment.poi.name,
              grounding: {
                status: "verified_amap",
                groundingStatus: "verified_amap",
                mapReady: true,
                routeable: true,
                matchedAmapName: segment.poi.name,
                hasProviderPoiId: true,
                hasCoordinates: true,
                needsVerification: false
              }
            }
          };
        })
      }
    ]
  };

  renderTimeline(lifecyclePlan);

  expect(screen.getByText("高德 POI 待校验")).toBeTruthy();
  expect(screen.getByText("已匹配地图锚点，待核对")).toBeTruthy();
  expect(screen.getByText("已确认高德地点")).toBeTruthy();
  expect(screen.getByText("景点费用待预约确认")).toBeTruthy();
  expect(screen.getAllByText("预约未查询").length).toBeGreaterThan(0);
  expect(screen.queryByText(/占位 POI 未完成地图 grounding/)).toBeNull();
  expect(screen.getByText("已匹配地图锚点，待核对").textContent).not.toContain("已确认高德地点");
});

test("timeline shows area and functional POI specificity states", () => {
  const base = threeSegmentPlanFixture();
  const specificityPlan = {
    ...base,
    days: [
      {
        ...base.days[0],
        segments: base.days[0].segments.map((segment, index) => {
          if (index === 0) {
            return {
              ...segment,
              poi: {
                ...segment.poi,
                name: "奥林匹克公园夜景",
                source: "agent-text-timeline",
                groundingStatus: "composite_poi",
                poiSpecificity: "composite_poi",
                intentType: "experience",
                needsConcretePoi: true,
                mapReady: true,
                routeable: true,
                matchedAmapName: "奥林匹克公园",
                grounding: {
                  ...segment.poi.grounding,
                  status: "composite_poi",
                  groundingStatus: "composite_poi",
                  poiSpecificity: "composite_poi",
                  intentType: "experience",
                  needsConcretePoi: true,
                  mapReady: true,
                  routeable: true,
                  matchedAmapName: "奥林匹克公园",
                  hasProviderPoiId: true,
                  hasCoordinates: true,
                  needsVerification: true
                }
              }
            };
          }
          if (index === 1) {
            return {
              ...segment,
              poi: {
                ...segment.poi,
                name: "晚餐",
                amapId: undefined,
                latitude: null,
                longitude: null,
                source: "agent-text-timeline",
                groundingStatus: "functional_poi",
                poiSpecificity: "functional_poi",
                intentType: "dining",
                needsConcretePoi: true,
                mapReady: false,
                routeable: false,
                grounding: {
                  status: "functional_poi",
                  groundingStatus: "functional_poi",
                  poiSpecificity: "functional_poi",
                  intentType: "dining",
                  needsConcretePoi: true,
                  mapReady: false,
                  routeable: false,
                  hasProviderPoiId: false,
                  hasCoordinates: false,
                  needsVerification: true
                }
              }
            };
          }
          return segment;
        })
      }
    ]
  };

  renderTimeline(specificityPlan);

  expect(screen.getByText("建议细化具体目标")).toBeTruthy();
  expect(screen.getByText("等待 Agent 按附近搜索补全")).toBeTruthy();
});

test("timeline expands concrete POI candidates and replaces from selected candidate", async () => {
  const base = planFixture();
  const candidatePoi = amapPoiFixture();
  const areaPlan: ItineraryPlan = {
    ...base,
    days: [
      {
        ...base.days[0],
        segments: base.days[0].segments.map((segment, index) =>
          index === 0
            ? {
                ...segment,
                poi: {
                  ...segment.poi,
                  name: "奥林匹克公园夜景",
                  source: "agent-text-timeline",
                  sourceNote: "已匹配高德地图锚点，路线可用，仍需用户确认是否为目标地点。POI意图：composite_poi；地图锚点：奥林匹克公园；建议选择具体场馆/入口/区域后再查询票务预约。",
                  groundingStatus: "composite_poi",
                  poiSpecificity: "composite_poi",
                  intentType: "experience",
                  needsConcretePoi: true,
                  mapReady: true,
                  routeable: true,
                  matchedAmapName: "奥林匹克公园",
                  grounding: {
                    ...segment.poi.grounding,
                    status: "composite_poi",
                    groundingStatus: "composite_poi",
                    poiSpecificity: "composite_poi",
                    intentType: "experience",
                    needsConcretePoi: true,
                    mapReady: true,
                    routeable: true,
                    matchedAmapName: "奥林匹克公园",
                    hasProviderPoiId: true,
                    hasCoordinates: true,
                    needsVerification: true
                  }
                }
              }
            : segment
        )
      }
    ]
  };
  const fetchMock = vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => {
    const body = JSON.parse(String(init?.body ?? "{}"));
    const operation = body.operations[0];
    if (operation.op === "expand_area_poi_candidates") {
      return jsonResponse({
        itinerary: areaPlan,
        patch: { id: "patch_expand", validationStatus: "accepted" },
        version: { id: "ver_expand", versionNumber: 1, sourceType: "manual" },
        validationErrors: [],
        pendingPoiCandidates: [
          {
            id: "cand_area_1",
            query: "奥林匹克公园夜景",
            city: "北京",
            category: "scenic",
            status: "pending",
            candidates: [candidatePoi],
            sourceSegmentId: "seg_1",
            createdAt: "2026-06-10T10:00:00Z"
          }
        ]
      });
    }
    if (operation.op === "replace_segment_poi_from_candidate") {
      return jsonResponse({
        itinerary: {
          ...areaPlan,
          days: areaPlan.days.map((day) => ({
            ...day,
            segments: day.segments.map((segment) =>
              segment.id === operation.segmentId ? { ...segment, poi: { ...segment.poi, ...candidatePoi, amapId: candidatePoi.id } } : segment
            )
          }))
        },
        patch: { id: "patch_replace", validationStatus: "accepted" },
        version: { id: "ver_replace", versionNumber: 2, sourceType: "manual" },
        validationErrors: [],
        pendingPoiCandidates: []
      });
    }
    return jsonResponse({}, 400);
  });
  vi.stubGlobal("fetch", fetchMock);
  renderTimeline(areaPlan, "seg_1");
  plannerStore.setState({
    selectedMapPoi: { ...candidatePoi, id: "stale_selected_poi", name: "旧地图候选" },
    candidateMapPois: [{ ...candidatePoi, id: "stale_candidate", name: "旧候选列表" }]
  });

  fireEvent.click(screen.getByText("展开具体候选"));
  await waitFor(() => expect(screen.getByText("故宫旁咖啡馆")).toBeTruthy());
  const expandBody = JSON.parse(String(fetchMock.mock.calls[0][1]?.body ?? "{}"));
  expect(expandBody.operations[0]).toEqual(
    expect.objectContaining({ op: "expand_area_poi_candidates", segmentId: "seg_1", radius: 1500 })
  );
  expect(plannerStore.getSnapshot().pendingPoiCandidates[0]?.id).toBe("cand_area_1");
  expect(plannerStore.getSnapshot().candidateMapPois).toEqual([]);
  expect(plannerStore.getSnapshot().selectedMapPoi).toBeNull();
  expect(plannerStore.getSnapshot().selectedSegmentId).toBe("seg_1");

  fireEvent.click(screen.getByText("故宫旁咖啡馆"));
  await waitFor(() => expect(screen.getByText("已将「奥林匹克公园夜景」替换为「故宫旁咖啡馆」，路线和票务状态已刷新。")));
  const replaceBody = JSON.parse(String(fetchMock.mock.calls[1][1]?.body ?? "{}"));
  expect(replaceBody.operations[0]).toEqual(
    expect.objectContaining({
      op: "replace_segment_poi_from_candidate",
      segmentId: "seg_1",
      candidateId: "cand_area_1"
    })
  );
  expect(plannerStore.getSnapshot().pendingPoiCandidates).toEqual([]);
  expect(plannerStore.getSnapshot().candidateMapPois).toEqual([]);
  expect(plannerStore.getSnapshot().selectedMapPoi).toBeNull();
  expect(plannerStore.getSnapshot().selectedSegmentId).toBe("seg_1");
  expect(plannerStore.getSnapshot().itineraryPlan?.days[0].segments[0].poi.name).toBe("故宫旁咖啡馆");
});

test("ordinary meal expands nearby dining candidates with meal-specific patch operation", async () => {
  const base = planFixture();
  const candidatePoi = amapPoiFixture();
  const mealSegment: PlannerSegment = {
    id: "seg_lunch",
    startTime: "12:00",
    endTime: "13:00",
    kind: "meal",
    poi: {
      id: "poi_lunch",
      name: "午餐",
      city: "北京",
      category: "meal",
      latitude: null,
      longitude: null,
      source: "agent-text-timeline",
      confidence: 0.35,
      type: "餐饮时间",
      district: "",
      address: "",
      sourceNote: "普通用餐时间；可选择顺路餐厅。mealGrounding=optional_waiting; requiredGrounding=false; pendingMeal=true；groundingStatus：optional_waiting；intentType：meal；routeAnchor=false；needsConcretePoi=true。",
      groundingStatus: "optional_waiting",
      mapReady: false,
      routeable: false,
      needsConcretePoi: true
    },
    transportMode: "public_transit",
    estimatedCost: 80,
    notes: "普通用餐时间；可选择顺路餐厅，不强制餐厅 grounding。"
  };
  const mealPlan: ItineraryPlan = {
    ...base,
    days: [{ ...base.days[0], segments: [base.days[0].segments[0], mealSegment, base.days[0].segments[1]] }]
  };
  const fetchMock = vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => {
    const body = JSON.parse(String(init?.body ?? "{}"));
    const operation = body.operations[0];
    if (operation.op === "expand_meal_poi_candidates") {
      return jsonResponse({
        itinerary: mealPlan,
        patch: { id: "patch_meal_expand", validationStatus: "accepted" },
        version: { id: "ver_meal_expand", versionNumber: 1, sourceType: "manual" },
        validationErrors: [],
        pendingPoiCandidates: [
          {
            id: "cand_meal_1",
            query: "午餐附近餐厅",
            city: "北京",
            category: "food",
            status: "pending",
            candidates: [candidatePoi],
            sourceSegmentId: "seg_lunch",
            createdAt: "2026-06-10T10:00:00Z"
          }
        ]
      });
    }
    return jsonResponse({}, 400);
  });
  vi.stubGlobal("fetch", fetchMock);
  renderTimeline(mealPlan, "seg_lunch");

  expect(screen.getByText("用餐时间 · 可选择顺路餐厅")).toBeTruthy();
  expect(screen.getByText("餐饮预算约 ¥80")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "选择顺路餐厅" }));
  await waitFor(() => expect(screen.getByText("故宫旁咖啡馆")).toBeTruthy());
  const expandBody = JSON.parse(String(fetchMock.mock.calls[0][1]?.body ?? "{}"));
  expect(expandBody.operations[0]).toEqual(
    expect.objectContaining({ op: "expand_meal_poi_candidates", segmentId: "seg_lunch", radius: 2200 })
  );
  expect(plannerStore.getSnapshot().pendingPoiCandidates[0]?.id).toBe("cand_meal_1");
});

test("timeline POI replacement refreshes and retries once after stale base version", async () => {
  const base = planFixture();
  const candidatePoi = amapPoiFixture();
  const areaPlan: ItineraryPlan = {
    ...base,
    days: [
      {
        ...base.days[0],
        segments: base.days[0].segments.map((segment, index) =>
          index === 0
            ? {
                ...segment,
                poi: {
                  ...segment.poi,
                  name: "奥林匹克公园夜景",
                  source: "agent-text-timeline",
                  groundingStatus: "composite_poi",
                  poiSpecificity: "composite_poi",
                  needsConcretePoi: true,
                  mapReady: true,
                  routeable: true
                }
              }
            : segment
        )
      }
    ]
  };
  const refreshedPlan = { ...areaPlan, title: "已刷新版本的北京行程" };
  const replacedPlan = {
    ...refreshedPlan,
    days: refreshedPlan.days.map((day) => ({
      ...day,
      segments: day.segments.map((segment) =>
        segment.id === "seg_1" ? { ...segment, poi: { ...segment.poi, ...candidatePoi, amapId: candidatePoi.id } } : segment
      )
    }))
  };
  const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    if (path.endsWith("/api/agent/sessions/current")) {
      return jsonResponse(agentSessionFixture("sess_timeline_retry", "刷新后的会话", refreshedPlan, "替换 POI", "ver_latest"));
    }
    const body = JSON.parse(String(init?.body ?? "{}"));
    const operation = body.operations[0];
    if (operation.op === "expand_area_poi_candidates") {
      return jsonResponse({
        itinerary: areaPlan,
        patch: { id: "patch_expand", validationStatus: "accepted" },
        version: { id: "ver_expand", versionNumber: 1, sourceType: "manual" },
        validationErrors: [],
        pendingPoiCandidates: [
          {
            id: "cand_area_1",
            query: "奥林匹克公园夜景",
            city: "北京",
            category: "scenic",
            status: "pending",
            candidates: [candidatePoi],
            sourceSegmentId: "seg_1",
            createdAt: "2026-06-10T10:00:00Z"
          }
        ]
      });
    }
    if (operation.op === "replace_segment_poi_from_candidate" && body.baseVersionId === "ver_expand") {
      return jsonResponse({ detail: "Base itinerary version is stale" }, 409);
    }
    if (operation.op === "replace_segment_poi_from_candidate") {
      expect(body.baseVersionId).toBe("ver_latest");
      return jsonResponse({
        itinerary: replacedPlan,
        patch: { id: "patch_replace_retry", validationStatus: "accepted" },
        version: { id: "ver_replace_retry", versionNumber: 3, sourceType: "manual" },
        validationErrors: [],
        pendingPoiCandidates: []
      });
    }
    return jsonResponse({}, 400);
  });
  vi.stubGlobal("fetch", fetchMock);
  renderTimeline(areaPlan, "seg_1");

  fireEvent.click(screen.getByText("展开具体候选"));
  await waitFor(() => expect(screen.getByText("故宫旁咖啡馆")).toBeTruthy());
  fireEvent.click(screen.getByText("故宫旁咖啡馆"));

  await waitFor(() => expect(screen.getByText("已同步最新行程并保存本次 POI 更新。")).toBeTruthy());
  expect(fetchMock.mock.calls.some((call) => String(call[0]).endsWith("/api/agent/sessions/current"))).toBe(true);
  expect(plannerStore.getSnapshot().activeVersionId).toBe("ver_replace_retry");
  expect(plannerStore.getSnapshot().itineraryPlan?.days[0].segments[0].poi.name).toBe("故宫旁咖啡馆");
});

test("does not offer concrete candidate expansion for already concrete POIs", () => {
  const plan = planFixture();
  const concretePlan: ItineraryPlan = {
    ...plan,
    days: plan.days.map((day) => ({
      ...day,
      segments: day.segments.map((segment) =>
        segment.id === "seg_1"
          ? {
              ...segment,
              poi: {
                ...segment.poi,
                source: "amap-place",
                amapId: "amap_exact_1",
                groundingStatus: "user_confirmed",
                poiSpecificity: "exact_entity",
                needsConcretePoi: true,
                grounding: {
                  status: "user_confirmed",
                  groundingStatus: "user_confirmed",
                  poiSpecificity: "exact_entity",
                  needsConcretePoi: true,
                  mapReady: true,
                  hasProviderPoiId: true,
                  hasCoordinates: true,
                  needsVerification: false
                }
              }
            }
          : segment
      )
    }))
  };

  renderTimeline(concretePlan, "seg_1");

  expect(screen.queryByText("展开具体候选")).toBeNull();
  expect(screen.queryByText("请选择具体场馆/入口/区域")).toBeNull();
});

test("offers concrete candidate expansion for area POIs even when needs flag is missing", () => {
  const plan = planFixture();
  const areaPlan: ItineraryPlan = {
    ...plan,
    days: plan.days.map((day) => ({
      ...day,
      segments: day.segments.map((segment) =>
        segment.id === "seg_1"
          ? {
              ...segment,
              poi: {
                ...segment.poi,
                groundingStatus: "area_poi",
                poiSpecificity: "area_poi",
                needsConcretePoi: undefined,
                grounding: {
                  status: "area_poi",
                  groundingStatus: "area_poi",
                  poiSpecificity: "area_poi",
                  needsConcretePoi: undefined,
                  mapReady: true,
                  hasProviderPoiId: true,
                  hasCoordinates: true,
                  needsVerification: true
                }
              }
            }
          : segment
      )
    }))
  };

  renderTimeline(areaPlan, "seg_1");

  expect(screen.getByText("展开具体候选")).toBeTruthy();
  expect(screen.getByText("请选择具体场馆/入口/区域")).toBeTruthy();
});

test("timeline uses meal-specific cost labels", () => {
  const base = planFixture();
  const mealSegment: PlannerSegment = {
    id: "seg_meal_unknown_cost",
    startTime: "12:00",
    endTime: "13:00",
    kind: "meal",
    poi: {
      id: "poi_meal_unknown_cost",
      name: "午餐",
      city: "北京",
      category: "meal",
      latitude: null,
      longitude: null,
      source: "agent-text-timeline",
      confidence: 0.35,
      type: "餐饮时间",
      district: "",
      address: "",
      sourceNote: "普通用餐时间；可选择顺路餐厅。mealGrounding=optional_waiting; requiredGrounding=false; pendingMeal=true；groundingStatus：optional_waiting；intentType：meal；routeAnchor=false；needsConcretePoi=true。",
      groundingStatus: "optional_waiting",
      mapReady: false,
      routeable: false,
      needsConcretePoi: true
    },
    transportMode: "public_transit",
    estimatedCost: 0,
    notes: "普通用餐时间；可选择顺路餐厅，不强制餐厅 grounding。"
  };
  const paidMeal = {
    ...mealSegment,
    id: "seg_meal_paid",
    poi: {
      ...mealSegment.poi,
      id: "poi_meal_paid",
      name: "北京本地菜餐厅",
      source: "amap-place-search",
      latitude: 39.92,
      longitude: 116.4,
      groundingStatus: "agent_selected_candidate",
      mapReady: true,
      routeable: true
    },
    estimatedCost: 160,
    notes: "costBasis=per_person; costPerPerson=80; partySize=2; totalCost=160; costSource=budget_policy"
  };
  const allowanceMeal = {
    ...mealSegment,
    id: "seg_meal_allowance",
    poi: { ...mealSegment.poi, id: "poi_meal_allowance", name: "晚餐" },
    estimatedCost: 240,
    notes: "costBasis=per_person; costPerPerson=120; partySize=2; totalCost=240; costSource=budget_policy"
  };
  const scenicUnknown = {
    ...base.days[0].segments[0],
    id: "seg_scenic_unknown",
    estimatedCost: 0
  };
  const plan = {
    ...base,
    days: [
      {
        ...base.days[0],
        segments: [scenicUnknown, mealSegment, paidMeal, allowanceMeal]
      }
    ]
  };

  render(
    <DailyTimeline
      plan={plan}
      selectedDayNumber={1}
      selectedSegmentId="seg_meal_unknown_cost"
      onSelectSegment={() => undefined}
      onTransportChange={() => undefined}
    />
  );

  expect(screen.getByText("餐饮费用待估算")).toBeTruthy();
  expect(screen.getByText("人均约 ¥80 · 合计约 ¥160")).toBeTruthy();
  expect(screen.getByText("人均预算约 ¥120 · 合计预算约 ¥240")).toBeTruthy();
  expect(screen.getByText("景点费用待预约确认")).toBeTruthy();
});

test("timeline shows required meal waiting state as pending completion", () => {
  const base = planFixture();
  const requiredMeal: PlannerSegment = {
    id: "seg_required_meal_waiting",
    startTime: "12:00",
    endTime: "13:00",
    kind: "meal",
    poi: {
      id: "poi_required_meal_waiting",
      name: "午餐 当地特色美食",
      city: "北京",
      category: "meal",
      latitude: null,
      longitude: null,
      source: "agent-text-timeline",
      confidence: 0.35,
      type: "餐饮地点待补全",
      district: "",
      address: "需要地图候选补全具体餐厅",
      sourceNote:
        "requiredGrounding=true; groundingRequiredReason=explicit_food_experience; groundingStatus：waiting_for_poi_grounding；needsConcretePoi=true；routeAnchor=true",
      groundingStatus: "waiting_for_poi_grounding",
      mapReady: false,
      routeable: false,
      needsConcretePoi: true
    },
    transportMode: "public_transit",
    estimatedCost: 80,
    notes:
      "requiredGrounding=true; groundingRequiredReason=explicit_food_experience; groundingStatus：waiting_for_poi_grounding；needsConcretePoi=true；routeAnchor=true"
  };
  const plan = {
    ...base,
    days: [{ ...base.days[0], segments: [base.days[0].segments[0], requiredMeal, base.days[0].segments[1]] }]
  };

  render(
    <DailyTimeline
      plan={plan}
      selectedDayNumber={1}
      selectedSegmentId="seg_required_meal_waiting"
      onSelectSegment={() => undefined}
      onTransportChange={() => undefined}
    />
  );

  expect(screen.getByText("餐饮地点待补全")).toBeTruthy();
  expect(screen.getByText("请选择具体餐厅/餐饮地点")).toBeTruthy();
  expect(screen.getByRole("button", { name: "选择顺路餐厅" })).toBeTruthy();
  expect(screen.queryByText("地图状态：not_required")).toBeNull();
});

test("timeline shows truthful route cost labels", () => {
  const zeroCostPlan = {
    ...planFixture(),
    routeOptions: planFixture().routeOptions.map((route) =>
      route.id === "route_1" ? { ...route, mode: "walking", transportMode: "walking", label: "步行", costAmount: 0, costEstimate: 0 } : route
    )
  };
  const errorRoutePlan = {
    ...planFixture(),
    routeOptions: planFixture().routeOptions.map((route) =>
      route.id === "route_1"
        ? {
            ...route,
            costAmount: 0,
            costEstimate: 0,
            error: { status: "needs_verification", reason: "missing_coordinates" }
          }
        : route
    )
  };

  renderTimeline(zeroCostPlan, "seg_1");
  expect(screen.getAllByText("免费").length).toBeGreaterThan(0);
  expect(screen.queryByText("免费/待计费")).toBeNull();
  cleanup();

  renderTimeline(errorRoutePlan, "seg_1");
  expect(screen.getByText("路线待地点确认后生成。")).toBeTruthy();
  expect(screen.queryByText("免费/待计费")).toBeNull();
});

test("timeline groups driving and taxi candidates and sends route group metadata when selected", async () => {
  const baseRoute = planFixture().routeOptions[0];
  const drivingTaxiPlan = {
    ...planFixture(),
    routeOptions: [
      {
        ...baseRoute,
        id: "route_driving",
        mode: "driving",
        transportMode: "driving",
        label: "驾车",
        isSelected: false,
        costAmount: 18,
        costEstimate: 18,
        durationSeconds: 960,
        durationMinutes: 16
      },
      {
        ...baseRoute,
        id: "route_taxi",
        mode: "taxi",
        transportMode: "taxi",
        label: "打车",
        isSelected: false,
        sortOrder: 2,
        costAmount: 32,
        costEstimate: 32,
        durationSeconds: 900,
        durationMinutes: 15
      }
    ]
  };
  const selectedPlan = {
    ...drivingTaxiPlan,
    routeOptions: drivingTaxiPlan.routeOptions.map((route) => ({
      ...route,
      isSelected: route.id === "route_taxi"
    }))
  };
  const fetchMock = vi.fn(async (_url: RequestInfo | URL, _init?: RequestInit) =>
    jsonResponse({
      itinerary: selectedPlan,
      patch: {
        id: "patch_route_group",
        validationStatus: "accepted",
        metadata: {
          routeGroup: {
            label: "驾车/打车",
            rawRouteIds: ["route_driving", "route_taxi"],
            representativeRouteId: "route_taxi"
          }
        }
      },
      version: { id: "ver_route_group", versionNumber: 2, sourceType: "manual" },
      validationErrors: [],
      planningRun: planningRunFixture("route_select"),
      pendingPoiCandidates: []
    })
  );
  vi.stubGlobal("fetch", fetchMock);

  renderTimeline(drivingTaxiPlan, "seg_1");

  expect(screen.getAllByText("驾车/打车").length).toBeGreaterThan(0);
  expect(screen.getAllByText("¥32").length).toBeGreaterThan(0);
  expect(screen.queryByText("驾车")).toBeNull();
  expect(screen.queryByText("打车")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "使用此路线" }));

  await waitFor(() => expect(fetchMock).toHaveBeenCalled());
  expect(String(fetchMock.mock.calls[0][0])).toContain("/api/itineraries/plan_workspace/routes/route_taxi/select");
  const body = JSON.parse(String(fetchMock.mock.calls[0][1]?.body));
  expect(body.planningContext.routeGroup).toMatchObject({
    label: "驾车/打车",
    representativeRouteId: "route_taxi",
    selectedRawMode: "taxi"
  });
  expect(body.planningContext.routeGroup.rawRouteIds.sort()).toEqual(["route_driving", "route_taxi"]);
  expect(body.planningContext.routeGroup.modes.sort()).toEqual(["driving", "taxi"]);
});

test("timeline route summary masks low risk when route risk source is not reliable", () => {
  const plan = {
    ...planFixture(),
    routeOptions: planFixture().routeOptions.map((route) =>
      route.id === "route_1"
        ? {
            ...route,
            crowdingRisk: "low",
            source: "mock-map-provider",
            provider: "mock-map-provider"
          }
        : route
    )
  };

  renderTimeline(plan, "seg_1");

  expect(screen.getByText("风险：风险待核验")).toBeTruthy();
  expect(screen.queryByText("风险：低风险")).toBeNull();
});

test("cancels trip title editing without saving", () => {
  const fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  renderTimeline();

  fireEvent.click(screen.getByText("北京地图行程草案"));
  fireEvent.change(screen.getByLabelText("旅行标题输入"), { target: { value: "不会保存的标题" } });
  fireEvent.click(screen.getByText("取消"));

  expect(screen.getByText("北京地图行程草案")).toBeTruthy();
  expect(screen.queryByText("不会保存的标题")).toBeNull();
  expect(fetchMock).not.toHaveBeenCalled();
});

test("edits day title through patch API, collapses and expands a day card", async () => {
  mockPatchFetch((plan, operations) => ({
    ...plan,
    days: plan.days.map((day) => (day.id === operations[0].dayId ? { ...day, title: String(operations[0].value ?? "") } : day))
  }));
  renderTimeline();

  fireEvent.click(screen.getByText("历史中轴线与老北京风情"));
  fireEvent.change(screen.getByLabelText("Day 1 标题输入"), { target: { value: "" } });
  fireEvent.click(screen.getByText("确认"));
  expect(screen.getByText("Day 标题不能为空。")).toBeTruthy();

  fireEvent.change(screen.getByLabelText("Day 1 标题输入"), {
    target: { value: "这是一个超过三十二个字的每日行程标题用于验证基础校验不能通过并且应该被拒绝保存" }
  });
  fireEvent.click(screen.getByText("确认"));
  expect(screen.getByText("Day 标题不能超过 32 个字。")).toBeTruthy();

  fireEvent.change(screen.getByLabelText("Day 1 标题输入"), { target: { value: "故宫与胡同体验" } });
  fireEvent.click(screen.getByText("确认"));
  await waitFor(() => expect(screen.getByText("故宫与胡同体验")).toBeTruthy());

  fireEvent.click(screen.getByText("折叠"));
  expect(screen.queryByText("深度游览，门票/预约状态待查询")).toBeNull();
  expect(screen.getAllByText(/已知路线 1\.9 km · 1\/1 路段已生成/).length).toBeGreaterThan(0);
  expect(screen.getAllByText("活动/用餐停留 3 小时0 分钟").length).toBeGreaterThan(0);
  expect(screen.getByText("花费 ¥44")).toBeTruthy();

  fireEvent.click(screen.getByText("展开"));
  expect(screen.getByText("深度游览，门票/预约状态待查询")).toBeTruthy();
});

test("edits segment time through patch API with validation", async () => {
  const fetchMock = mockPatchFetch((plan, operations) => ({
    ...plan,
    days: plan.days.map((day) => ({
      ...day,
      segments: day.segments.map((segment) =>
        segment.id === operations[0].segmentId
          ? { ...segment, startTime: String(operations[0].value ?? segment.startTime), endTime: "10:30" }
          : segment
      )
    }))
  }));
  renderTimeline();

  const timeButton = screen.getByRole("button", { name: "编辑 故宫博物院 到达时间" });
  expect(timeButton.className).toContain("segment-time-pill");
  fireEvent.click(timeButton);
  const timeInput = screen.getByLabelText("故宫博物院 时间输入") as HTMLInputElement;
  expect(timeInput.type).toBe("time");
  expect(screen.getByText("到达时间")).toBeTruthy();

  fireEvent.change(timeInput, { target: { value: "11:45" } });
  fireEvent.click(screen.getByText("确认"));
  expect(screen.getByText("时间与下一项冲突，请保留足够间隔。")).toBeTruthy();

  fireEvent.change(timeInput, { target: { value: "08:30" } });
  fireEvent.click(screen.getByText("确认"));
  await waitFor(() => expect(screen.getByText("08:30")).toBeTruthy());
  const patchBody = JSON.parse(String(fetchMock.mock.calls[0][1]?.body));
  expect(patchBody.operations).toEqual([{ op: "replace_segment_start_time", segmentId: "seg_1", value: "08:30" }]);
});

test("keeps time draft through a same-itinerary facts refresh and confirms exactly once", async () => {
  const fetchMock = mockPatchFetch((plan, operations) => applyFixtureOperations(plan, operations));
  const plan = planFixture();
  const props = { selectedDayNumber: 1, selectedSegmentId: null, onSelectSegment: () => undefined };
  plannerStore.setState({ itineraryPlan: plan, activeVersionId: "ver_workspace" });
  const view = render(<DailyTimeline {...props} plan={plan} />);
  fireEvent.click(screen.getByRole("button", { name: "编辑 故宫博物院 到达时间" }));
  fireEvent.change(screen.getByLabelText("故宫博物院 时间输入"), { target: { value: "08:30" } });
  const refreshed = { ...plan, routeWarnings: ["事实刷新待补充，不改变行程时间"] };
  plannerStore.setState({ itineraryPlan: refreshed });
  view.rerender(<DailyTimeline {...props} plan={refreshed} />);
  expect((screen.getByLabelText("故宫博物院 时间输入") as HTMLInputElement).value).toBe("08:30");
  expect(fetchMock).not.toHaveBeenCalled();
  fireEvent.click(screen.getByText("确认"));
  await waitFor(() => expect(screen.queryByLabelText("故宫博物院 时间输入")).toBeNull());
  expect(fetchMock).toHaveBeenCalledTimes(1);
  const body = JSON.parse(String(fetchMock.mock.calls[0][1]?.body));
  expect(body.baseVersionId).toBe("ver_workspace");
  expect(body.operations).toEqual([{ op: "replace_segment_start_time", segmentId: "seg_1", value: "08:30" }]);
});

test.each(["plan", "day", "target", "poi", "source-time", "end-time", "read-only"])("invalidates a time draft after authoritative %s change", (change) => {
  const fetchMock = vi.spyOn(globalThis, "fetch");
  const plan = planFixture();
  const props = { selectedDayNumber: 1, selectedSegmentId: null, onSelectSegment: () => undefined };
  const view = render(<DailyTimeline {...props} plan={plan} />);
  fireEvent.click(screen.getByRole("button", { name: "编辑 故宫博物院 到达时间" }));
  fireEvent.change(screen.getByLabelText("故宫博物院 时间输入"), { target: { value: "08:30" } });
  const updated: ItineraryPlan = {
    ...plan,
    id: change === "plan" ? "different_plan" : plan.id,
    days: plan.days.map((day) => ({
      ...day,
      id: change === "day" ? `${day.id}_moved` : day.id,
      segments: day.segments
        .filter((segment) => change !== "target" || segment.id !== "seg_1")
        .map((segment) => segment.id === "seg_1" ? {
          ...segment,
          poi: change === "poi" ? { ...segment.poi, id: "replacement_poi" } : segment.poi,
          startTime: change === "source-time" ? "08:45" : segment.startTime,
          endTime: change === "end-time" ? "10:45" : segment.endTime
        } : segment)
    }))
  };
  view.rerender(<DailyTimeline {...props} plan={updated} readOnly={change === "read-only"} />);
  expect(screen.queryByLabelText("故宫博物院 时间输入")).toBeNull();
  expect(fetchMock).not.toHaveBeenCalled();
});

test("adds activity and day through patch API and updates totals", async () => {
  mockPatchFetch((plan, operations) => applyFixtureOperations(plan, operations));
  renderTimeline();

  expect(screen.getAllByText("活动/用餐停留 3 小时0 分钟").length).toBeGreaterThan(0);
  fireEvent.click(screen.getByText("+ 添加景点/活动"));
  await waitFor(() => expect(screen.getByText("故宫旁咖啡馆")).toBeTruthy());
  expect(screen.getAllByText("活动/用餐停留 3 小时30 分钟").length).toBeGreaterThan(0);
  expect(plannerStore.getSnapshot().selectedSegmentId).toBe("seg_added");

  fireEvent.click(screen.getByText("+ 增加日程安排"));
  await waitFor(() => expect(screen.getByText("Day 2")).toBeTruthy());
  expect(screen.getByText("待规划日程")).toBeTruthy();
  expect(screen.queryByText("生成 Agent 上下文")).toBeNull();
  expect(screen.getByText("统计来自当前右侧时间栏，Agent 对话会自动携带最新上下文。")).toBeTruthy();
});

test("removes a timeline segment, recalculates totals, and selects the adjacent item", async () => {
  vi.spyOn(window, "confirm").mockReturnValue(true);
  const fetchMock = mockPatchFetch((plan, operations) => applyFixtureOperations(plan, operations));
  renderTimeline(threeSegmentPlanFixture(), "seg_2");
  plannerStore.setState({
    selectedMapPoi: amapPoiFixture(),
    candidateMapPois: [amapPoiFixture()],
    selectedRouteOptionId: "route_1",
    previewRouteOptionId: "route_2"
  });

  fireEvent.click(screen.getByRole("button", { name: "删除 景山公园" }));

  await waitFor(() => expect(screen.getByText("已删除景点/活动，路线已重新规划或标记为待重新规划。")).toBeTruthy());
  const patchBody = JSON.parse(String(fetchMock.mock.calls[0][1]?.body));
  expect(patchBody.operations).toEqual([{ op: "remove_segment", segmentId: "seg_2" }]);
  expect(screen.queryByText("景山公园")).toBeNull();
  expect(screen.getAllByText("活动/用餐停留 3 小时0 分钟").length).toBeGreaterThan(0);
  expect(screen.getByText("当前可估（暂估） ¥40")).toBeTruthy();
  expect(plannerStore.getSnapshot().selectedSegmentId).toBe("seg_3");
  expect(plannerStore.getSnapshot().candidateMapPois).toEqual([]);
  expect(plannerStore.getSnapshot().selectedMapPoi).toBeNull();
  expect(plannerStore.getSnapshot().selectedRouteOptionId).toBeNull();
  expect(plannerStore.getSnapshot().previewRouteOptionId).toBeNull();
});

test("reorders same-day segments through patch API and syncs Agent context", async () => {
  const fetchMock = mockPatchFetch((plan, operations) => applyFixtureOperations(plan, operations));
  plannerStore.setState({
    preferenceCard: {
      id: "pref_timeline",
      profileId: "pref_profile_timeline",
      partySize: 2,
      travelerTypes: [],
      budgetRange: "3000 元",
      pacePreference: "轻松不赶路",
      summaryText: "用户偏好轻松不赶路，公共交通优先。",
      items: [],
      status: "confirmed"
    }
  });
  renderTimeline(threeSegmentPlanFixture(), "seg_2");

  const target = screen.getByLabelText("拖放到 故宫博物院");
  mockDragTargetRect(target, 0);
  fireEvent.dragStart(screen.getByLabelText("拖动 北海公园"));
  fireDragOverAt(target, 10);
  expect(screen.getByText("放到此项上方")).toBeTruthy();
  fireDropAt(target, 10);

  await waitFor(() => {
    const patchBody = JSON.parse(String(fetchMock.mock.calls[0][1]?.body));
    expect(patchBody.operations[0]).toMatchObject({
      op: "reorder_segments",
      dayId: "day_1",
      orderedSegmentIds: ["seg_3", "seg_1", "seg_2"]
    });
    expect(patchBody.preferenceSummary).toBe("用户偏好轻松不赶路，公共交通优先。");
    expect(patchBody.planningContext).toMatchObject({
      city: "北京",
      currentPreferenceSummary: "用户偏好轻松不赶路，公共交通优先。",
      itineraryPlanId: "plan_workspace",
      selectedSegmentId: "seg_3",
      patchIntent: "reorder_segments"
    });
  });
  const segmentNames = screen.getAllByTestId("timeline-segment-name").map((node) => node.textContent);
  expect(segmentNames).toEqual(["北海公园", "故宫博物院", "景山公园"]);
  expect(plannerStore.getSnapshot().itineraryAgentContext?.days[0].segments.map((segment) => segment.poiName)).toEqual([
    "北海公园",
    "故宫博物院",
    "景山公园"
  ]);
  expect(
    screen
      .getAllByRole("button", { name: /编辑 .* 到达时间/ })
      .map((button) => button.querySelector("strong")?.textContent)
  ).toEqual([
    "09:30",
    "11:30",
    "14:30"
  ]);
});

test("drag cue supports inserting a segment below the target item", async () => {
  const fetchMock = mockPatchFetch((plan, operations) => applyFixtureOperations(plan, operations));
  renderTimeline(threeSegmentPlanFixture(), "seg_3");

  const target = screen.getByLabelText("拖放到 故宫博物院");
  mockDragTargetRect(target, 0);
  fireEvent.dragStart(screen.getByLabelText("拖动 北海公园"));
  fireDragOverAt(target, 90);
  expect(screen.getByText("放到此项下方")).toBeTruthy();
  fireDropAt(target, 90);

  await waitFor(() => {
    const patchBody = JSON.parse(String(fetchMock.mock.calls[0][1]?.body));
    expect(patchBody.operations[0]).toMatchObject({
      op: "reorder_segments",
      dayId: "day_1",
      orderedSegmentIds: ["seg_1", "seg_3", "seg_2"]
    });
  });
  const segmentNames = screen.getAllByTestId("timeline-segment-name").map((node) => node.textContent);
  expect(segmentNames).toEqual(["故宫博物院", "北海公园", "景山公园"]);
});

test("route selection sends preference context and stores the returned Planning Run", async () => {
  const plan = planWithAlternativeRouteFixture();
  const updatedPlan = {
    ...plan,
    routeOptions: plan.routeOptions.map((route) => {
      if (route.id === "route_alt") {
        return { ...route, isSelected: true };
      }
      if (route.id === "route_1") {
        return { ...route, isSelected: false };
      }
      return route;
    })
  };
  const planningRun = planningRunFixture("route_select");
  const fetchMock = vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) =>
    jsonResponse({
      itinerary: updatedPlan,
      patch: { id: "patch_route", validationStatus: "accepted" },
      version: { id: "ver_route", versionNumber: 2, sourceType: "manual" },
      validationErrors: [],
      planningRun,
      pendingPoiCandidates: []
    })
  );
  vi.stubGlobal("fetch", fetchMock);
  plannerStore.setState({
    preferenceCard: {
      id: "pref_card",
      profileId: "pref_profile",
      partySize: 2,
      travelerTypes: [],
      budgetRange: "3000 元",
      pacePreference: "轻松不赶路",
      summaryText: "用户偏好轻松不赶路，公共交通优先，但长距离可接受打车。",
      items: [],
      status: "confirmed"
    }
  });
  renderTimeline(plan, "seg_1");

  fireEvent.click(screen.getByRole("button", { name: "使用此路线" }));

  await waitFor(() => expect(fetchMock).toHaveBeenCalled());
  const body = JSON.parse(String(fetchMock.mock.calls[0][1]?.body));
  expect(String(fetchMock.mock.calls[0][0])).toContain("/api/itineraries/plan_workspace/routes/route_alt/select");
  expect(body.preferenceSummary).toBe("用户偏好轻松不赶路，公共交通优先，但长距离可接受打车。");
  expect(body.planningContext).toMatchObject({
    city: "北京",
    currentPreferenceSummary: "用户偏好轻松不赶路，公共交通优先，但长距离可接受打车。",
    itineraryPlanId: "plan_workspace",
    selectedRouteOptionId: "route_alt",
    selectedSegmentId: "seg_1"
  });
  expect(plannerStore.getSnapshot().lastPlanningRun?.runType).toBe("route_select");
  expect(screen.getByText("当前可估（暂估） ¥40")).toBeTruthy();
  expect(screen.getAllByText(/已知路线 6\.8 km · 1\/1 路段已生成/).length).toBeGreaterThan(0);
  await waitFor(() =>
    expect(plannerStore.getSnapshot().itineraryAgentContext?.tripTotals).toMatchObject({
      durationMinutes: 180,
      estimatedCost: 68,
      walkingDistanceMeters: 6800
    })
  );
  expect(screen.getByText("已保存路线选择。")).toBeTruthy();
});

test("optimize routes button calls backend and applies recomputed schedule", async () => {
  const plan = planWithAlternativeRouteFixture();
  const updatedPlan = {
    ...plan,
    days: plan.days.map((day) => ({
      ...day,
      segments: day.segments.map((segment) =>
        segment.id === "seg_2"
          ? { ...segment, startTime: "11:25", endTime: "12:25" }
          : segment
      )
    })),
    routeOptions: plan.routeOptions.map((route) => {
      if (route.id === "route_alt") {
        return { ...route, isSelected: true };
      }
      if (route.id === "route_1") {
        return { ...route, isSelected: false };
      }
      return route;
    })
  };
  const fetchMock = vi.fn(async (_url: RequestInfo | URL, _init?: RequestInit) =>
    jsonResponse({
      itinerary: updatedPlan,
      patch: {
        id: "patch_optimize",
        validationStatus: "accepted",
        metadata: {
          routeOptimization: { changedCount: 1 },
          schedulePolicy: "computed_from_selected_routes",
          scheduleUpdatedCount: 1
        }
      },
      version: { id: "ver_optimize", versionNumber: 3, sourceType: "manual" },
      validationErrors: [],
      planningRun: planningRunFixture("route_optimize"),
      pendingPoiCandidates: []
    })
  );
  vi.stubGlobal("fetch", fetchMock);
  renderTimeline(plan, "seg_1");

  fireEvent.click(screen.getByRole("button", { name: "优化路线" }));
  expect(screen.getByRole("menuitem", { name: "综合最优" })).toBeTruthy();
  expect(screen.getByRole("menuitem", { name: "时间最短" })).toBeTruthy();
  expect(screen.getByRole("menuitem", { name: "费用最少" })).toBeTruthy();
  fireEvent.click(screen.getByRole("menuitem", { name: "时间最短" }));

  await waitFor(() => expect(fetchMock).toHaveBeenCalled());
  expect(String(fetchMock.mock.calls[0][0])).toContain("/api/itineraries/plan_workspace/routes/optimize");
  const body = JSON.parse(String(fetchMock.mock.calls[0][1]?.body));
  expect(body.planningContext).toMatchObject({
    selectedSegmentId: "seg_1",
    scheduleOnly: false,
    optimizationObjective: "fastest"
  });
  expect(body.optimizationObjective).toBe("fastest");
  await waitFor(() => expect(screen.getByText("11:25")).toBeTruthy());
  expect(screen.getByText("已按“时间最短”自动切换 1 段路线，并重新排时。")).toBeTruthy();
  expect(plannerStore.getSnapshot().lastPlanningRun?.runType).toBe("route_optimize");
});

test("auto schedule button recomputes schedule without changing route preference", async () => {
  const plan = planFixture();
  const updatedPlan = {
    ...plan,
    days: plan.days.map((day) => ({
      ...day,
      segments: day.segments.map((segment) =>
        segment.id === "seg_2"
          ? { ...segment, startTime: "11:58", endTime: "12:58" }
          : segment
      )
    }))
  };
  const fetchMock = vi.fn(async (_url: RequestInfo | URL, _init?: RequestInit) =>
    jsonResponse({
      itinerary: updatedPlan,
      patch: {
        id: "patch_schedule",
        validationStatus: "accepted",
        metadata: { routeOptimization: { changedCount: 0 }, scheduleUpdatedCount: 1 }
      },
      version: { id: "ver_schedule", versionNumber: 3, sourceType: "manual" },
      validationErrors: [],
      planningRun: planningRunFixture("route_optimize"),
      pendingPoiCandidates: []
    })
  );
  vi.stubGlobal("fetch", fetchMock);
  renderTimeline(plan, "seg_1");

  fireEvent.click(screen.getByRole("button", { name: "自动排期" }));

  await waitFor(() => expect(fetchMock).toHaveBeenCalled());
  const body = JSON.parse(String(fetchMock.mock.calls[0][1]?.body));
  expect(body.planningContext.scheduleOnly).toBe(true);
  await waitFor(() => expect(screen.getByText("11:58")).toBeTruthy());
  expect(screen.getByText("已按当前选中路线重新排时。")).toBeTruthy();
});

test("route selection anchors planning context to the route leg being changed", async () => {
  const plan = planWithSecondLegAlternativeRouteFixture();
  const updatedPlan = {
    ...plan,
    routeOptions: plan.routeOptions.map((route) => {
      if (route.id === "route_second_alt") {
        return { ...route, isSelected: true };
      }
      if (route.id === "route_3") {
        return { ...route, isSelected: false };
      }
      return route;
    })
  };
  const fetchMock = vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) =>
    jsonResponse({
      itinerary: updatedPlan,
      patch: { id: "patch_second_route", validationStatus: "accepted" },
      version: { id: "ver_second_route", versionNumber: 2, sourceType: "manual" },
      validationErrors: [],
      planningRun: planningRunFixture("route_select"),
      pendingPoiCandidates: []
    })
  );
  vi.stubGlobal("fetch", fetchMock);
  renderTimeline(plan, "seg_1");

  fireEvent.click(screen.getByText("展开路线"));
  fireEvent.click(screen.getByRole("button", { name: "使用此路线" }));

  await waitFor(() => expect(fetchMock).toHaveBeenCalled());
  const body = JSON.parse(String(fetchMock.mock.calls[0][1]?.body));
  expect(String(fetchMock.mock.calls[0][0])).toContain("/api/itineraries/plan_workspace/routes/route_second_alt/select");
  expect(body.planningContext).toMatchObject({
    selectedRouteOptionId: "route_second_alt",
    selectedSegmentId: "seg_2",
    routeOption: expect.objectContaining({
      id: "route_second_alt",
      fromSegmentId: "seg_2",
      toSegmentId: "seg_3"
    })
  });
  expect(plannerStore.getSnapshot().selectedSegmentId).toBe("seg_2");
});

test("route selection recovers from a 409 by refreshing the current session and retrying the same safe route once", async () => {
  const plan = planWithAlternativeRouteFixture();
  const refreshedPlan = {
    ...plan,
    title: "服务端最新行程",
    routeOptions: plan.routeOptions.map((route) => ({ ...route }))
  };
  const selectedPlan = {
    ...refreshedPlan,
    routeOptions: refreshedPlan.routeOptions.map((route) => {
      if (route.id === "route_alt") {
        return { ...route, isSelected: true };
      }
      if (route.id === "route_1") {
        return { ...route, isSelected: false };
      }
      return route;
    })
  };
  const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    if (path.endsWith("/api/itineraries/plan_workspace/routes/route_alt/select")) {
      const selectCalls = fetchMock.mock.calls.filter((call) => String(call[0]).includes("/routes/route_alt/select")).length;
      if (selectCalls === 1) {
        return jsonResponse({ detail: "base version is stale" }, 409);
      }
      return jsonResponse({
        itinerary: selectedPlan,
        patch: { id: "patch_retry", validationStatus: "accepted" },
        version: { id: "ver_after_retry", versionNumber: 3, sourceType: "manual" },
        validationErrors: [],
        planningRun: planningRunFixture("route_select"),
        pendingPoiCandidates: []
      });
    }
    if (path.endsWith("/api/agent/sessions/current")) {
      return jsonResponse(agentSessionFixture("sess_route_conflict", "路线冲突恢复会话", refreshedPlan, "切换路线", "ver_latest"));
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);
  renderTimeline(plan, "seg_1");
  plannerStore.setState({ activeVersionId: "ver_stale" });

  fireEvent.click(screen.getByRole("button", { name: "使用此路线" }));

  await waitFor(() => expect(screen.getByText("已同步最新行程并保存路线选择。")).toBeTruthy());
  expect(fetchMock.mock.calls.filter((call) => String(call[0]).includes("/routes/route_alt/select")).length).toBe(2);
  expect(fetchMock.mock.calls.some((call) => String(call[0]).endsWith("/api/agent/sessions/current"))).toBe(true);
  const firstBody = JSON.parse(String(fetchMock.mock.calls[0][1]?.body));
  const retryBody = JSON.parse(String(fetchMock.mock.calls[2][1]?.body));
  expect(firstBody.baseVersionId).toBe("ver_stale");
  expect(retryBody.baseVersionId).toBe("ver_latest");
  expect(plannerStore.getSnapshot().agentSession?.activeVersionId).toBe("ver_after_retry");
  expect(plannerStore.getSnapshot().activeVersionId).toBe("ver_after_retry");
  expect(plannerStore.getSnapshot().selectedRouteOptionId).toBe("route_alt");
});

test("route selection refreshes after 409 but does not retry when the route leg is no longer safe to match", async () => {
  const plan = planWithAlternativeRouteFixture();
  const refreshedPlan = {
    ...plan,
    title: "路线候选已变化",
    routeOptions: plan.routeOptions.filter((route) => route.id !== "route_alt" && route.id !== "route_1")
  };
  const fetchMock = vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.includes("/routes/route_alt/select")) {
      return jsonResponse({ detail: "base version is stale" }, 409);
    }
    if (path.endsWith("/api/agent/sessions/current")) {
      return jsonResponse(agentSessionFixture("sess_route_conflict", "路线冲突恢复会话", refreshedPlan, "切换路线", "ver_latest_changed"));
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);
  renderTimeline(plan, "seg_1");
  plannerStore.setState({ activeVersionId: "ver_stale" });

  fireEvent.click(screen.getByRole("button", { name: "使用此路线" }));

  await waitFor(() => expect(screen.getByText("行程已更新，路线候选已刷新，请基于最新路线重新选择。")).toBeTruthy());
  expect(fetchMock.mock.calls.filter((call) => String(call[0]).includes("/routes/route_alt/select")).length).toBe(1);
  expect(plannerStore.getSnapshot().itineraryPlan?.title).toBe("路线候选已变化");
  expect(plannerStore.getSnapshot().activeVersionId).toBe("ver_latest_changed");
  expect(plannerStore.getSnapshot().selectedRouteOptionId).toBeNull();
});

test("route selection 409 recovery does not retry without a refreshed active version id", async () => {
  const plan = planWithAlternativeRouteFixture();
  const fetchMock = vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.includes("/routes/route_alt/select")) {
      return jsonResponse({ detail: "base version is stale" }, 409);
    }
    if (path.endsWith("/api/agent/sessions/current")) {
      return jsonResponse(agentSessionFixture("sess_route_conflict", "路线冲突恢复会话", plan, "切换路线", null));
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);
  renderTimeline(plan, "seg_1");
  plannerStore.setState({ activeVersionId: "ver_stale" });

  fireEvent.click(screen.getByRole("button", { name: "使用此路线" }));

  await waitFor(() => expect(screen.getByText("行程已更新，路线候选已刷新，请基于最新路线重新选择。")).toBeTruthy());
  expect(fetchMock.mock.calls.filter((call) => String(call[0]).includes("/routes/route_alt/select")).length).toBe(1);
  expect(plannerStore.getSnapshot().activeVersionId).toBeNull();
});

test("route selection 409 recovery retries at most once", async () => {
  const plan = planWithAlternativeRouteFixture();
  const fetchMock = vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.includes("/routes/route_alt/select")) {
      return jsonResponse({ detail: "base version is stale again" }, 409);
    }
    if (path.endsWith("/api/agent/sessions/current")) {
      return jsonResponse(agentSessionFixture("sess_route_conflict", "路线冲突恢复会话", plan, "切换路线", "ver_latest"));
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);
  renderTimeline(plan, "seg_1");
  plannerStore.setState({ activeVersionId: "ver_stale" });

  fireEvent.click(screen.getByRole("button", { name: "使用此路线" }));

  await waitFor(() => expect(screen.getByText("行程已再次更新，路线候选已刷新，请基于最新路线重新选择。")).toBeTruthy());
  expect(fetchMock.mock.calls.filter((call) => String(call[0]).includes("/routes/route_alt/select")).length).toBe(2);
  expect(fetchMock.mock.calls.filter((call) => String(call[0]).endsWith("/api/agent/sessions/current")).length).toBe(1);
  expect(plannerStore.getSnapshot().activeVersionId).toBe("ver_latest");
});

test("reorder failure rolls back timeline order and shows a clear error", async () => {
  const originalPlan = threeSegmentPlanFixture();
  plannerStore.setState({ itineraryPlan: originalPlan, activeVersionId: "ver_reorder", lastPatchError: "" });
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => jsonResponse({ detail: { validationErrors: ["orderedSegmentIds must include every segment in the day exactly once"] } }, 400))
  );
  renderTimeline(originalPlan, "seg_2");

  const target = screen.getByLabelText("拖放到 故宫博物院");
  mockDragTargetRect(target, 0);
  fireEvent.dragStart(screen.getByLabelText("拖动 北海公园"));
  fireDragOverAt(target, 10);
  fireDropAt(target, 10);

  await waitFor(() =>
    expect(screen.getByText(/orderedSegmentIds must include every segment in the day exactly once/)).toBeTruthy()
  );
  const segmentNames = screen.getAllByTestId("timeline-segment-name").map((node) => node.textContent);
  expect(segmentNames).toEqual(["故宫博物院", "景山公园", "北海公园"]);
  expect(plannerStore.getSnapshot().itineraryPlan?.days[0].segments.map((segment) => segment.poi.name)).toEqual([
    "故宫博物院",
    "景山公园",
    "北海公园"
  ]);
});

test("prompts for a selected AMap POI before adding a timeline activity", async () => {
  const fetchMock = mockPatchFetch((plan, operations) => applyFixtureOperations(plan, operations));
  renderTimeline();
  plannerStore.setState({ selectedMapPoi: null });

  fireEvent.click(screen.getByText("+ 添加景点/活动"));

  expect(screen.getByText("请先在地图中选择一个高德 POI，再添加到行程。")).toBeTruthy();
  expect(fetchMock).not.toHaveBeenCalled();
  expect(screen.queryByText("待定景点/活动")).toBeNull();
});

test("shows weather data status, confidence, and failure reason", () => {
  renderTimeline({
    ...planFixture(),
    weatherSignals: [
      {
        ...planFixture().weatherSignals[0],
        dailySummary: "待 Agent 查询",
        riskLevel: "unavailable",
        purposeImpactReason: "天气服务暂不可用，当前天气风险判断不完整。",
        source: "高德天气",
        dataStatus: "fallback",
        confidence: 0.5,
        failureReason: "AMAP_WEB_SERVICE_KEY is not configured.",
        userVisibleCaveat: "高德天气 provider 不可用，已使用 mock 天气结果；出行前请重新查询真实天气。",
        providerName: "mock-amap-weather-provider",
        fallbackUsed: true
      }
    ]
  });

  expect(screen.queryByText(/天气预警/)).toBeNull();
  const riskSignals = within(screen.getByLabelText("Weather and crowding signals"));
  expect(
    riskSignals.getByText((_content, element) =>
      Boolean(element?.classList.contains("risk-signals-summary") && element.textContent?.includes("2026/06/04 · 待 Agent 查询"))
    )
  ).toBeTruthy();
  fireEvent.click(riskSignals.getByRole("button", { name: "展开风险" }));
  expect(screen.getByText("天气服务暂不可用，当前天气风险判断不完整。")).toBeTruthy();
  fireEvent.click(riskSignals.getByRole("button", { name: "收起风险" }));
  expect(screen.queryByText("天气服务暂不可用，当前天气风险判断不完整。")).toBeNull();
  fireEvent.click(riskSignals.getByRole("button", { name: "展开风险" }));
  const weatherReminder = riskSignals.getByText("天气").closest("details") as HTMLDetailsElement;
  expect(weatherReminder.open).toBe(false);
  fireEvent.click(screen.getByText("天气"));
  expect(weatherReminder.open).toBe(true);
  expect(screen.getByText(/天气日期.*2026/)).toBeTruthy();
  expect(screen.getByText("状态：已降级 · 风险等级：不可用 · 置信度 50%")).toBeTruthy();
  expect(screen.getByText("天气查询已降级：天气服务暂时不可用，请稍后重新查询或检查服务配置。")).toBeTruthy();
  expect(screen.queryByText(/AMAP_WEB_SERVICE_KEY/)).toBeNull();
  expect(screen.getByText("天气服务暂时不可用，当前天气风险判断不完整；出行前请重新查询真实天气。")).toBeTruthy();
  expect(screen.queryByText(/provider/)).toBeNull();
  expect(screen.queryByText(/mock 天气结果/)).toBeNull();
});

test("risk fallback or mock low-like states are shown as pending verification", () => {
  renderTimeline({
    ...planFixture(),
    weatherSignals: [
      {
        ...planFixture().weatherSignals[0],
        dailySummary: "晴，待核验",
        riskLevel: "ideal",
        purposeImpactReason: "天气服务暂不可用，当前天气风险判断不完整。",
        source: "mock-weather-provider",
        providerName: "mock-weather-provider",
        dataStatus: "fallback",
        fallbackUsed: true,
        failureReason: "provider unavailable"
      }
    ],
    trafficCrowdingSignals: [
      {
        ...planFixture().trafficCrowdingSignals[0],
        realDataAvailable: false,
        crowdingLevel: "low",
        recommendedDepartureAdjustment: "按计划出发",
        source: "mock-traffic-provider"
      }
    ]
  });

  const riskSignals = within(screen.getByLabelText("Weather and crowding signals"));
  expect(
    riskSignals.getByText((_content, element) =>
      Boolean(element?.classList.contains("risk-signals-summary") && element.textContent?.includes("景点风险：风险待核验"))
    )
  ).toBeTruthy();
  expect(riskSignals.queryByText(/低风险/)).toBeNull();
  fireEvent.click(riskSignals.getByRole("button", { name: "展开风险" }));
  fireEvent.click(riskSignals.getByText("天气"));
  expect(screen.getByText("状态：已降级 · 风险等级：风险待核验")).toBeTruthy();
});

test("timeline shows initial ticket placeholders as not queried", () => {
  const plan = {
    ...planFixture(),
    ticketLookupResults: [
      {
        id: "ticket_pending",
        segmentId: "seg_1",
        ticketType: "reservation",
        status: "not_checked",
        priceEstimate: 0,
        bookingUrl: "",
        sourceName: "预约未查询",
        sourceUrl: "",
        credibilityRank: "unavailable",
        caveat: "初始行程未全量查询预约状态；可在需要时手动刷新。",
        providerName: "local-ticket-guard",
        fallbackUsed: false,
        confidence: 0,
        queriedAt: "2026-06-10T10:00:00Z"
      }
    ]
  };

  renderTimeline(plan);

  expect(screen.getAllByText("预约未查询").length).toBeGreaterThan(0);
  expect(screen.queryByText("not_checked")).toBeNull();
  const ticketArea = screen.getByText("预约").closest(".segment-ticket-source") as HTMLElement;
  fireEvent.click(within(ticketArea).getByText("预约"));
  expect(within(ticketArea).getByText(/初始行程未全量查询预约状态/)).toBeTruthy();
  expect(within(ticketArea).queryByRole("link")).toBeNull();
});

test("timeline shows concrete POI requirement for area ticket guard", () => {
  const plan = {
    ...planFixture(),
    ticketLookupResults: [
      {
        id: "ticket_area_guard",
        segmentId: "seg_1",
        ticketType: "reservation",
        status: "needs_concrete_poi",
        priceEstimate: 0,
        bookingUrl: "",
        sourceName: "需要选择具体场馆/入口/区域",
        sourceUrl: "",
        credibilityRank: "unavailable",
        queriedAt: "2026-06-10T10:00:00Z",
        caveat: "请选择具体场馆/入口/区域后再查询预约。开放区域未发现统一预约入口，节假日管控需以官方公告为准。",
        providerName: "local-ticket-guard",
        fallbackUsed: false,
        confidence: 0
      }
    ]
  };

  renderTimeline(plan);

  const ticketArea = screen.getByText("预约").closest(".segment-ticket-source") as HTMLElement;
  expect(within(ticketArea).getByText("请选择具体地点")).toBeTruthy();
  fireEvent.click(within(ticketArea).getByText("预约"));
  expect(within(ticketArea).getByText(/请选择具体场馆\/入口\/区域/)).toBeTruthy();
  expect(within(ticketArea).queryByRole("link")).toBeNull();
});

test("shows compact clickable ticket source links without exposing raw URLs", () => {
  renderTimeline({
    ...planFixture(),
    ticketLookupResults: [
      {
        id: "ticket_1",
        segmentId: "seg_1",
        ticketType: "reservation",
        status: "available",
        priceEstimate: 0,
        bookingUrl: "https://tickets.example.com/palace?ref=long",
        sourceName: "https://tickets.example.com/palace?ref=long",
        sourceUrl: "https://tickets.example.com/palace?ref=long",
        credibilityRank: "official",
        queriedAt: "2026-06-10T10:00:00Z",
        caveat: "请以官方渠道确认为准。",
        providerName: "bocha-web-search",
        fallbackUsed: false,
        providerFailureReason: undefined,
        confidence: 0.86
      }
    ]
  });

  const ticketArea = screen.getByText("预约").closest(".segment-ticket-source") as HTMLElement;
  const link = within(ticketArea).getByRole("link", { name: /打开tickets\.example\.com预约来源/ }) as HTMLAnchorElement;
  expect(link.textContent).toBe("查看官方预约入口");
  expect(link.getAttribute("href")).toBe("https://tickets.example.com/palace?ref=long");
  expect(link.getAttribute("title")).toBe("打开tickets.example.com预约来源");
  expect(within(ticketArea).queryByText("https://tickets.example.com/palace?ref=long")).toBeNull();

  fireEvent.click(within(ticketArea).getByText("预约"));
  expect(within(ticketArea).getByText("来源：tickets.example.com")).toBeTruthy();
  expect(within(ticketArea).getByText(/查询 2026\/6\/10 18:00:00 · 置信度 86%/)).toBeTruthy();
});

test("does not present a non-official search result as a reservation entry", () => {
  renderTimeline({
    ...planFixture(),
    ticketLookupResults: [
      {
        id: "ticket_supplemental",
        segmentId: "seg_1",
        ticketType: "reservation",
        status: "available",
        priceEstimate: 0,
        bookingUrl: "https://www.zhihu.com/question/campus-visit",
        sourceName: "知乎校园参观攻略",
        sourceUrl: "https://www.zhihu.com/question/campus-visit",
        credibilityRank: "search",
        queriedAt: "2026-06-10T10:00:00Z",
        caveat: "仅作为补充信息。",
        providerName: "web-search",
        fallbackUsed: false,
        confidence: 0.61
      }
    ]
  });

  const ticketArea = screen.getByText("预约").closest(".segment-ticket-source") as HTMLElement;
  expect(within(ticketArea).getAllByText("未找到官方入口").length).toBeGreaterThan(0);
  expect(within(ticketArea).queryByRole("link")).toBeNull();
  expect(ticketArea.textContent).not.toContain("购票");
});

test("normalizes schemeless ticket source names instead of showing raw URLs", () => {
  renderTimeline({
    ...planFixture(),
    ticketLookupResults: [
      {
        id: "ticket_1",
        segmentId: "seg_1",
        ticketType: "reservation",
        status: "available",
        priceEstimate: 0,
        bookingUrl: "",
        sourceName: "tickets.example.com/palace?ref=long",
        sourceUrl: "",
        credibilityRank: "official",
        queriedAt: "2026-06-10T10:00:00Z",
        caveat: "请以官方渠道确认为准。",
        providerName: "bocha-web-search",
        fallbackUsed: false,
        providerFailureReason: undefined,
        confidence: 0.72
      }
    ]
  });

  const ticketArea = screen.getByText("预约").closest(".segment-ticket-source") as HTMLElement;
  fireEvent.click(within(ticketArea).getByText("预约"));

  expect(within(ticketArea).getByText("来源：tickets.example.com")).toBeTruthy();
  expect(within(ticketArea).queryByText("tickets.example.com/palace?ref=long")).toBeNull();
});

test("shows segment ticket source metadata and provider failure state", () => {
  renderTimeline({
    ...planFixture(),
    ticketLookupResults: [
      {
        id: "ticket_1",
        segmentId: "seg_1",
        ticketType: "reservation",
        status: "unknown",
        priceEstimate: 0,
        bookingUrl: "",
        sourceName: "联网搜索失败",
        sourceUrl: "",
        credibilityRank: "unavailable",
        queriedAt: "2026-06-10T10:00:00Z",
        caveat: "联网搜索未返回可用真实来源，请以官方渠道确认为准。",
        providerName: "bocha-web-search",
        fallbackUsed: false,
        providerFailureReason: "WEB_SEARCH_API_KEY is not configured.",
        confidence: 0
      }
    ]
  });

  const ticketArea = screen.getByText("预约").closest(".segment-ticket-source") as HTMLElement;
  expect(within(ticketArea).getByText("查询失败/待确认")).toBeTruthy();
  expect(within(ticketArea).getByText("未找到官方入口")).toBeTruthy();
  fireEvent.click(within(ticketArea).getByText("预约"));

  expect(within(ticketArea).getByText("来源：联网搜索失败")).toBeTruthy();
  expect(within(ticketArea).getByText(/查询 2026\/6\/10 18:00:00 · 置信度 0%/)).toBeTruthy();
  expect(within(ticketArea).getByText(/查询失败：相关服务暂时不可用，当前结果可能不完整；请稍后重新查询或检查服务配置。/)).toBeTruthy();
  expect(screen.queryByText(/WEB_SEARCH_API_KEY/)).toBeNull();
  expect(screen.queryByText(/bocha-web-search/)).toBeNull();
  expect(screen.queryByText(/Provider/)).toBeNull();
});

test("shows POI risk search summary, source links, and incomplete failure state", () => {
  renderTimeline({
    ...planFixture(),
    poiRiskAlerts: [
      {
        id: "risk_1",
        planId: "plan_workspace",
        segmentId: "seg_1",
        poiName: "故宫博物院",
        status: "available",
        summary: "故宫博物院暑期参观需要提前预约，部分入口施工绕行。",
        sourceName: "搜索结果",
        sourceUrl: "https://example.com/palace-notice",
        sources: [
          {
            title: "故宫博物院预约公告",
            url: "https://example.com/palace-notice",
            snippet: "故宫博物院暑期参观需要提前预约。"
          }
        ],
        confidence: 0.72,
        failureReason: null,
        userVisibleCaveat: "风险提醒基于公开搜索结果，仍需以景区官方公告和现场管理为准。",
        queriedAt: "2026-06-10T10:00:00Z"
      },
      {
        id: "risk_2",
        planId: "plan_workspace",
        segmentId: "seg_2",
        poiName: "景山公园",
        status: "degraded",
        summary: "结合当前行程上下文（2人，预算 1200），公开来源提示：景山公园周边周末人流较大。 Agent 判断：公开来源足够提示错峰，但 Agent 风险判断服务暂不可用。",
        sourceName: "搜索结果",
        sourceUrl: "https://example.com/jingshan",
        sources: [{ title: "景山公园客流提示", url: "https://example.com/jingshan", snippet: "周末人流较大。" }],
        confidence: 0.6,
        failureReason: "Agent 风险判断服务暂不可用，已保留公开搜索摘要。",
        userVisibleCaveat: "Agent 风险判断服务暂不可用，已保留公开搜索摘要；风险判断不完整，请核对来源。",
        queriedAt: "2026-06-10T10:00:00Z"
      }
    ]
  });

  fireEvent.click(screen.getByRole("button", { name: "展开风险" }));
  expect(screen.getByText("景点风险搜索")).toBeTruthy();
  const riskArea = screen.getByLabelText("POI risk search alerts");
  const palaceRisk = within(riskArea).getByText("故宫博物院").closest("details") as HTMLDetailsElement;
  expect(palaceRisk.open).toBe(false);
  fireEvent.click(within(riskArea).getByText("故宫博物院"));
  fireEvent.click(within(riskArea).getByText("景山公园"));
  expect(palaceRisk.open).toBe(true);
  expect(screen.getByText("故宫博物院暑期参观需要提前预约，部分入口施工绕行。")).toBeTruthy();
  expect(screen.getByText("风险判断提示：Agent 风险判断服务暂不可用，已保留公开搜索摘要。")).toBeTruthy();
  expect(screen.getAllByText("查看来源 1").length).toBe(2);
});

test("aggregates route warnings and keeps raw provider details collapsed", () => {
  const plan = planFixture();
  renderTimeline({
    ...plan,
    days: plan.days.map((day) => ({
      ...day,
      segments: day.segments.map((segment) => ({
        ...segment,
        poi: {
          ...segment.poi,
          amapId: `amap_${segment.poi.id}`,
          source: "amap-place-search"
        }
      }))
    })),
    routeOptions: [
      ...plan.routeOptions,
      {
        ...plan.routeOptions[0],
        id: "route_stale_summary_only",
        fromSegmentId: "seg_old_from",
        toSegmentId: "seg_old_to"
      }
    ],
    routeWarnings: [
      "AMap POI resolve failed for 故宫博物院: MAP_PROVIDER_KEY is missing",
      "AMap POI resolve returned no candidates for 景山公园."
    ]
  });

  const warningArea = screen.getByRole("alert");
  expect(within(warningArea).getByText("已匹配 2 个地点，0 个地点待补全；已生成 1 段路线，0 段待补全。")).toBeTruthy();
  expect(within(warningArea).getByText("开发者详情")).toBeTruthy();
  expect(within(warningArea).getByText("AMap POI resolve failed for 故宫博物院: MAP_PROVIDER_KEY is missing")).toBeTruthy();
  expect(screen.queryByText("地图服务暂时无法确认部分地点或路线，请检查地图服务配置后重试。")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "展开风险" }));
  fireEvent.click(screen.getByText("天气"));
  fireEvent.click(screen.getByText("拥挤"));
  expect(screen.getByText(/来源：天气服务（真实数据待接入）/)).toBeTruthy();
  expect(screen.getByText(/来源：交通服务（真实数据待接入）/)).toBeTruthy();
});
test("shows patch failure without polluting planner store itinerary", async () => {
  const originalPlan = planFixture();
  plannerStore.setState({ itineraryPlan: originalPlan, activeVersionId: "ver_1", lastPatchError: "" });
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => jsonResponse({ detail: { validationErrors: ["server rejected title"] } }, 400))
  );
  renderTimeline(originalPlan);

  fireEvent.click(screen.getByText("北京地图行程草案"));
  fireEvent.change(screen.getByLabelText("旅行标题输入"), { target: { value: "北京失败标题" } });
  fireEvent.click(screen.getByText("确认"));

  await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("server rejected title"));
  expect(plannerStore.getSnapshot().itineraryPlan?.title).toBe("北京地图行程草案");
});

test("ignores stale timeline patch responses after active version moves forward", async () => {
  const stalePatch = deferred<Response>();
  const fetchMock = vi.fn(async () => stalePatch.promise);
  vi.stubGlobal("fetch", fetchMock);
  const originalPlan = planFixture();
  const newerPlan = { ...originalPlan, title: "更新后的服务端行程" };
  plannerStore.setState({
    itineraryPlan: originalPlan,
    activeVersionId: "ver_base",
    lastPatchError: "",
    selectedMapPoi: amapPoiFixture(),
    selectedSegmentId: null
  });
  render(
    <DailyTimeline
      plan={originalPlan}
      selectedDayNumber={1}
      selectedSegmentId={null}
      onSelectSegment={() => undefined}
      onTransportChange={() => undefined}
    />
  );

  fireEvent.click(screen.getByText("北京地图行程草案"));
  fireEvent.change(screen.getByLabelText("旅行标题输入"), { target: { value: "旧响应标题" } });
  fireEvent.click(screen.getByText("确认"));
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

  plannerStore.setState({ itineraryPlan: newerPlan, activeVersionId: "ver_new" });
  stalePatch.resolve(
    jsonResponse({
      itinerary: { ...originalPlan, title: "旧响应标题" },
      patch: { id: "patch_stale", validationStatus: "accepted" },
      version: { id: "ver_stale", versionNumber: 2, sourceType: "manual" },
      validationErrors: []
    })
  );
  await nextTick();

  expect(plannerStore.getSnapshot().activeVersionId).toBe("ver_new");
  expect(plannerStore.getSnapshot().itineraryPlan?.title).toBe("更新后的服务端行程");
});

test("ignores stale route selection responses after active version moves forward", async () => {
  const staleRoute = deferred<Response>();
  const fetchMock = vi.fn(async () => staleRoute.promise);
  vi.stubGlobal("fetch", fetchMock);
  const originalPlan = planWithAlternativeRouteFixture();
  const newerPlan = { ...originalPlan, title: "用户后续修改的行程" };
  plannerStore.setState({
    itineraryPlan: originalPlan,
    activeVersionId: "ver_route_base",
    lastPatchError: "",
    selectedMapPoi: amapPoiFixture(),
    selectedSegmentId: "seg_1",
    selectedRouteOptionId: "route_1"
  });
  render(
    <DailyTimeline
      plan={originalPlan}
      selectedDayNumber={1}
      selectedSegmentId="seg_1"
      onSelectSegment={() => undefined}
      onTransportChange={() => undefined}
    />
  );

  fireEvent.click(screen.getByRole("button", { name: "使用此路线" }));
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

  plannerStore.setState({ itineraryPlan: newerPlan, activeVersionId: "ver_route_new", selectedRouteOptionId: "route_1" });
  staleRoute.resolve(
    jsonResponse({
      itinerary: {
        ...originalPlan,
        routeOptions: originalPlan.routeOptions.map((route) =>
          route.id === "route_alt" ? { ...route, isSelected: true } : { ...route, isSelected: false }
        )
      },
      patch: { id: "patch_route_stale", validationStatus: "accepted" },
      version: { id: "ver_route_stale", versionNumber: 2, sourceType: "manual" },
      validationErrors: []
    })
  );
  await nextTick();

  expect(plannerStore.getSnapshot().activeVersionId).toBe("ver_route_new");
  expect(plannerStore.getSnapshot().itineraryPlan?.title).toBe("用户后续修改的行程");
  expect(plannerStore.getSnapshot().selectedRouteOptionId).toBe("route_1");
});

test("builds itinerary Agent context structure from editable days", () => {
  const days = createEditableDays(planFixture());
  const context = buildItineraryAgentContext("北京 1 日行程", days);
  const totals = calculateTripTotals(days);

  expect(context.tripTitle).toBe("北京 1 日行程");
  expect(context.days[0].dayTitle).toBe("历史中轴线与老北京风情");
  expect(context.days[0].segments[0]).toMatchObject({
    time: "09:30",
    poiName: "故宫博物院",
    agentNotes: "深度游览，门票/预约状态待查询",
    duration: 120,
    estimatedCost: 30
  });
  expect(context.tripTotals).toEqual(totals);
  expect(context.tripTotals).toMatchObject({
    durationMinutes: 180,
    activityDurationMinutes: 180,
    travelDurationMinutes: 18,
    bufferDurationMinutes: 0,
    scheduleSpanMinutes: 240
  });
});

test("separates explicit buffer, selected-route travel, and schedule span in totals and copy", () => {
  const plan = planFixture();
  plan.days[0].segments.push({
    ...plan.days[0].segments[1],
    id: "seg_buffer",
    kind: "buffer",
    startTime: "14:00",
    endTime: "14:30"
  });
  const days = createEditableDays(plan);
  const totals = calculateTripTotals(days);

  expect(totals).toMatchObject({
    durationMinutes: 210,
    activityDurationMinutes: 180,
    travelDurationMinutes: 18,
    bufferDurationMinutes: 30,
    scheduleSpanMinutes: 300,
    explicitBufferMinutes: 30,
    unallocatedGapMinutes: 72,
    requiredRouteLegCount: 1,
    coveredRouteLegCount: 1,
    unknownRouteLegCount: 0
  });
  plan.budgetTier = "medium";
  plan.budgetTarget = null;
  const copy = formatTimelineForCopy(plan, plan.title, days);
  expect(copy).toContain("活动/用餐停留：3 小时0 分钟");
  expect(copy).toContain("已知交通：18 分钟");
  expect(copy).toContain("显式缓冲：30 分钟");
  expect(copy).toContain("真实空档：1 小时12 分钟");
  expect(copy).toContain("日程跨度（各日合计）：5 小时0 分钟");
  expect(copy).toContain("预算档位：中等预算");
  expect(copy).toContain("金额上限：未指定");
  expect(copy).toContain("路线覆盖：1/1");
});

test("keeps known totals and marks transport partial when one required route is missing", () => {
  const plan = { ...planFixture(), routeOptions: [] };
  const totals = calculateTripTotals(createEditableDays(plan));
  const copy = formatTimelineForCopy(plan, plan.title, createEditableDays(plan));

  expect(totals).toMatchObject({
    requiredRouteLegCount: 1,
    coveredRouteLegCount: 0,
    unknownRouteLegCount: 1,
    walkingDistanceMeters: 0
  });
  expect(copy).toContain("路线覆盖：0/1");
  expect(copy).toContain("交通费用：部分估算");
  expect(copy).toContain("真实空档：1 小时0 分钟；待补路线 1 段");
});

test("syncs the editable right timeline as Agent context in planner store", async () => {
  mockPatchFetch((plan, operations) => ({
    ...plan,
    title: String(operations[0].value ?? plan.title)
  }));
  renderTimeline();

  await waitFor(() =>
    expect(plannerStore.getSnapshot().itineraryAgentContext).toMatchObject({
      tripTitle: "北京地图行程草案",
      days: [
        {
          dayTitle: "历史中轴线与老北京风情",
          segments: expect.arrayContaining([
            expect.objectContaining({
              time: "09:30",
              poiName: "故宫博物院",
              agentNotes: "深度游览，门票/预约状态待查询",
              duration: 120,
              estimatedCost: 30
            })
          ])
        }
      ],
      tripTotals: {
        durationMinutes: 180,
        estimatedCost: 44,
        walkingDistanceMeters: 1900
      }
    })
  );

  fireEvent.click(screen.getByText("北京地图行程草案"));
  fireEvent.change(screen.getByLabelText("旅行标题输入"), { target: { value: "北京 2 日行程" } });
  fireEvent.click(screen.getByText("确认"));

  await waitFor(() => expect(plannerStore.getSnapshot().itineraryAgentContext?.tripTitle).toBe("北京 2 日行程"));
});

test("cost tab renders grouped current-version cost breakdown", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: RequestInfo | URL) => {
      if (String(url).endsWith("/providers/status")) {
        return jsonResponse({ mode: "mock", default: [], mock: [] });
      }
      if (String(url).endsWith("/itineraries/plan_workspace/versions/saved")) {
        return jsonResponse({ savedVersions: [] });
      }
      return jsonResponse({}, 404);
    })
  );
  const plan = {
    ...planFixture(),
    budgetEstimate: 999,
    budgetBreakdown: {
      tier: "medium",
      numericTarget: null,
      knownActivityCost: 30,
      knownMealCost: 80,
      knownTransportCost: 24,
      knownTotal: 134,
      provisionalMin: 120,
      provisionalPreferred: 154,
      provisionalMax: 210,
      unknownItems: ["未核验门票/预约相关收费"],
      isComplete: false
    }
  };
  plannerStore.setState({ itineraryPlan: plan, activeVersionId: null, itineraryAgentContext: null, timelineCopyText: "" });

  render(<AppShell />);

  await waitFor(() => expect(plannerStore.getSnapshot().itineraryAgentContext?.tripTotals.estimatedCost).toBe(44));
  fireEvent.click(screen.getByText("费用明细"));

  expect(screen.queryByText("费用明细待接入")).toBeNull();
  const panel = within(screen.getByLabelText("费用明细"));
  expect(panel.getByText("餐饮")).toBeTruthy();
  expect(panel.getByText("交通")).toBeTruthy();
  expect(panel.getByText("景点/票务")).toBeTruthy();
  expect(panel.getByText("住宿")).toBeTruthy();
  expect(within(panel.getByLabelText("费用可信度汇总")).getByText("当前可估（暂估） ¥154")).toBeTruthy();
  expect(within(panel.getByLabelText("费用可信度汇总")).getByText("已知合计 ¥134")).toBeTruthy();
  expect(within(panel.getByLabelText("费用可信度汇总")).getByText("暂估范围 ¥120–¥210")).toBeTruthy();
  expect(within(panel.getByLabelText("费用可信度汇总")).getByText("未知 1 项")).toBeTruthy();
  expect(within(panel.getByLabelText("预算未知项")).getByText("未核验门票/预约相关收费")).toBeTruthy();
  expect(panel.getByText(/公交\/地铁 · 故宫博物院 -> 景山公园/)).toBeTruthy();
  expect(panel.getAllByText(/票务待查询/).length).toBeGreaterThan(0);
  expect(panel.getAllByText(/不计入/).length).toBeGreaterThan(0);
  const copy = formatTimelineForCopy(plan, plan.title, createEditableDays(plan));
  expect(copy).toContain("已知合计：¥134");
  expect(copy).toContain("当前可估（暂估）：¥154");
  expect(copy).toContain("暂估范围：¥120–¥210");
  expect(copy).toContain("未知项：未核验门票/预约相关收费");
  expect(copy).not.toContain("当前可估（暂估）：¥999");
  expect(copy).not.toContain("当前可估（暂估）：¥44");
});

test("risk signals are collapsed by default and expandable", () => {
  renderTimeline(planFixture(), "seg_1");

  expect(screen.getByRole("button", { name: "展开风险" })).toBeTruthy();
  expect(screen.queryByText("天气日期 2026/06/04")).toBeNull();

  fireEvent.click(screen.getByRole("button", { name: "展开风险" }));

  expect(screen.getByRole("button", { name: "收起风险" })).toBeTruthy();
  expect(screen.getByText(/天气日期/)).toBeTruthy();
});

test("route candidate panel exposes driving taxi refresh action", async () => {
  const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    if (String(url).endsWith("/itineraries/plan_workspace/patch")) {
      const body = JSON.parse(String(init?.body));
      expect(body.operations).toEqual([{ op: "refresh_routes_for_day", dayId: "day_1" }]);
      expect(body.planningContext.preferredRouteMode).toBe("transit");
      expect(body.planningContext.includeModes).toEqual(["driving", "taxi"]);
      const itinerary = {
        ...planFixture(),
        routeOptions: [
          ...planFixture().routeOptions,
          {
            ...planFixture().routeOptions[0],
            id: "route_drive",
            mode: "driving",
            label: "驾车",
            isSelected: false,
            sortOrder: 3,
            costAmount: 0,
            costEstimate: 0
          }
        ]
      };
      return jsonResponse({
        itinerary,
        patch: { id: "patch_drive", validationStatus: "accepted" },
        version: { id: "ver_drive", versionNumber: 2, sourceType: "manual" },
        validationErrors: [],
        pendingPoiCandidates: []
      });
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);
  renderTimeline(planFixture(), "seg_1");

  const expandRoute = screen.queryByRole("button", { name: "展开路线" });
  if (expandRoute) {
    fireEvent.click(expandRoute);
  }
  fireEvent.click(screen.getByRole("button", { name: "生成驾车/打车" }));

  await waitFor(() => expect(plannerStore.getSnapshot().activeVersionId).toBe("ver_drive"));
  expect(plannerStore.getSnapshot().itineraryPlan?.routeOptions.some((route) => route.mode === "driving")).toBe(true);
});

test("save current version and export buttons call backend APIs", async () => {
  const createObjectURL = vi.fn(() => "blob:trip-export");
  const revokeObjectURL = vi.fn();
  Object.defineProperty(URL, "createObjectURL", { configurable: true, value: createObjectURL });
  Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: revokeObjectURL });
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
  const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) {
      return jsonResponse({ mode: "mock", default: [], mock: [] });
    }
    if (path.endsWith("/itineraries/plan_workspace/versions/saved")) {
      return jsonResponse({ savedVersions: [] });
    }
    if (path.endsWith("/itineraries/plan_workspace/versions/ver_workspace/save")) {
      expect(init?.method).toBe("POST");
      return jsonResponse({
        id: "saved_1",
        sessionId: "sess_workspace",
        planId: "plan_workspace",
        versionId: "ver_workspace",
        title: "北京地图行程草案",
        createdAt: "2026-07-07T00:00:00Z"
      });
    }
    if (path.endsWith("/itineraries/plan_workspace/export?format=markdown")) {
      return new Response("# 北京地图行程草案\n", { status: 200, headers: { "Content-Type": "text/markdown" } });
    }
    if (path.endsWith("/itineraries/plan_workspace/export?format=json")) {
      return jsonResponse({
        exportedAt: "2026-07-07T00:00:00Z",
        activeVersionId: "ver_workspace",
        planId: "plan_workspace",
        itineraryPlan: planFixture(),
        routeOptions: planFixture().routeOptions,
        riskSignals: { weatherSignals: [], trafficCrowdingSignals: [], poiRiskAlerts: [] }
      });
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);
  plannerStore.setState({
    itineraryPlan: planFixture(),
    activeVersionId: "ver_workspace",
    agentSession: agentSessionFixture("sess_workspace", "北京地图行程草案", planFixture(), "北京一天", "ver_workspace")
  });

  render(<AppShell />);

  expect(screen.queryByRole("button", { name: "保存当前版本" })).toBeNull();
  expect(screen.queryByRole("button", { name: "导出 Markdown" })).toBeNull();

  fireEvent.click(screen.getByRole("button", { name: "保存" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "保存当前版本" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "已保存" })).toBeTruthy());

  fireEvent.click(screen.getByRole("button", { name: "导出" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "导出 Markdown" }));
  await waitFor(() => expect(createObjectURL).toHaveBeenCalledTimes(1));
  fireEvent.click(screen.getByRole("button", { name: "已导出" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "导出 JSON" }));

  await waitFor(() => expect(createObjectURL).toHaveBeenCalledTimes(2));
  expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining("/itineraries/plan_workspace/export?format=markdown"), expect.anything());
  expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining("/itineraries/plan_workspace/export?format=json"), expect.anything());
});

test("uses stable route colors per itinerary leg", () => {
  const plan = planFixture();
  const firstLegFast = plan.routeOptions[0];
  const firstLegAlternative = { ...plan.routeOptions[0], id: "route_1_alt", mode: "transit", label: "公交/地铁" };
  const secondLeg = plan.routeOptions[1];
  const colorMap = buildRouteLegColorMap(plan.days, [...plan.routeOptions, firstLegAlternative]);

  expect(routeLegColor(firstLegFast, colorMap)).toBe(routeLegColor(firstLegAlternative, colorMap));
  expect(routeLegColor(firstLegFast, colorMap)).not.toBe(routeLegColor(secondLeg, colorMap));
  expect(routeLegColor(firstLegFast, colorMap)).toBe(routeLegColor(firstLegFast, colorMap));
});

function renderTimeline(plan = planFixture(), selectedSegmentId: string | null = null) {
  plannerStore.setState({
    selectedCity: plan.city,
    itineraryPlan: plan,
    activeVersionId: "ver_workspace",
    lastPatchError: "",
    selectedMapPoi: amapPoiFixture(),
    selectedSegmentId
  });
  render(
    <DailyTimeline
      plan={plan}
      selectedDayNumber={1}
      selectedSegmentId={selectedSegmentId}
      onSelectSegment={() => undefined}
      onTransportChange={() => undefined}
    />
  );
}

function agentSessionFixture(
  sessionId: string,
  title: string,
  itinerary: ItineraryPlan,
  userMessage: string,
  versionId: string | null
): AgentSession {
  const createdAt = "2026-06-10T10:00:00Z";
  return {
    sessionId,
    status: "active",
    city: itinerary.city,
    title,
    activePlanId: itinerary.id,
    activeVersionId: versionId,
    turns: [
      {
        id: `${sessionId}_user`,
        role: "user",
        content: userMessage,
        turnIndex: 1,
        status: "active",
        createdAt,
        updatedAt: createdAt
      },
      {
        id: `${sessionId}_assistant`,
        role: "assistant",
        content: `已生成 ${itinerary.title}。`,
        turnIndex: 2,
        status: "active",
        itineraryVersionId: versionId ?? undefined,
        planningSteps: [],
        toolEvents: [],
        createdAt,
        updatedAt: createdAt
      }
    ],
    itinerary,
    pendingPoiCandidates: [],
    preferenceMemory: null
  };
}

function preferenceMemoryFixture(sessionId: string, memoryText: string) {
  return {
    userId: "default",
    sessionId,
    memoryText,
    autoUpdateEnabled: true,
    createdAt: "2026-06-10T10:00:00Z",
    updatedAt: "2026-06-10T10:00:00Z"
  };
}
function agentSessionSummary(session: ReturnType<typeof agentSessionFixture>) {
  return {
    sessionId: session.sessionId,
    title: session.title,
    city: session.city,
    status: session.status,
    activePlanId: session.activePlanId,
    activeVersionId: session.activeVersionId,
    turnCount: session.turns.length,
    createdAt: session.turns[0].createdAt,
    updatedAt: session.turns[session.turns.length - 1].updatedAt
  };
}

function mockPatchFetch(apply: (plan: ItineraryPlan, operations: Array<Record<string, unknown>>) => ItineraryPlan) {
  const fetchMock = vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => {
    const body = JSON.parse(String(init?.body ?? "{}")) as { operations: Array<Record<string, unknown>> };
    const itinerary = apply(plannerStore.getSnapshot().itineraryPlan ?? planFixture(), body.operations);
    plannerStore.setState({ itineraryPlan: itinerary });
    return jsonResponse({
      itinerary,
      patch: { id: "patch_test", validationStatus: "accepted" },
      version: { id: `ver_${Date.now()}`, versionNumber: 1, sourceType: "manual" },
      validationErrors: [],
      pendingPoiCandidates: []
    });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function applyFixtureOperations(plan: ItineraryPlan, operations: Array<Record<string, unknown>>): ItineraryPlan {
  let next = plan;
  for (const operation of operations) {
    if (operation.op === "add_segment") {
      const amapPoi = operation.amapPoi as ReturnType<typeof amapPoiFixture> | undefined;
      next = {
        ...next,
        days: next.days.map((day) =>
          day.id === operation.dayId
            ? {
                ...day,
                totalEstimatedCost: day.totalEstimatedCost,
                segments: [
                  ...day.segments,
                  {
                    id: "seg_added",
                    startTime: String(operation.startTime),
                    endTime: "14:30",
                    kind: "activity",
                    poi: {
                      id: "poi_added",
                      name: amapPoi?.name ?? String(operation.title ?? "待定景点/活动"),
                      city: amapPoi?.city ?? "北京",
                      category: amapPoi?.category ?? "pending",
                      latitude: amapPoi?.latitude ?? 39.9042,
                      longitude: amapPoi?.longitude ?? 116.4074,
                      source: amapPoi?.source ?? "itinerary-skeleton",
                      confidence: amapPoi?.confidence ?? 0
                    },
                    transportMode: "walk",
                    estimatedCost: 0,
                    notes: String(operation.notes)
                  }
                ]
              }
            : day
        )
      };
    }
    if (operation.op === "reorder_segments") {
      next = {
        ...next,
        days: next.days.map((day) =>
          day.id === operation.dayId
            ? {
                ...day,
                segments: reorderFixtureSegments(day.segments, operation.orderedSegmentIds as string[])
              }
            : day
        )
      };
    }
    if (operation.op === "add_day") {
      next = {
        ...next,
        days: [
          ...next.days,
          {
            id: "day_2",
            dayNumber: 2,
            title: String(operation.title),
            weatherSummary: "",
            riskSummary: "",
            totalEstimatedCost: 0,
            segments: []
          }
        ]
      };
    }
    if (operation.op === "remove_segment") {
      next = {
        ...next,
        days: next.days.map((day) => ({
          ...day,
          segments: day.segments.filter((segment) => segment.id !== operation.segmentId)
        })),
        routeOptions: next.routeOptions.filter(
          (route) => route.fromSegmentId !== operation.segmentId && route.toSegmentId !== operation.segmentId
        )
      };
    }
  }
  return next;
}

function reorderFixtureSegments(segments: ItineraryPlan["days"][number]["segments"], orderedSegmentIds: string[]) {
  if (!segments.length) {
    return [];
  }
  const segmentsById = new Map(segments.map((segment) => [segment.id, segment]));
  let currentStart = clockToMinutes(segments[0].startTime);
  const gapsAfterSlot = segments
    .slice(0, -1)
    .map((segment, index) => Math.max(0, clockToMinutes(segments[index + 1].startTime) - clockToMinutes(segment.endTime)));
  return orderedSegmentIds.map((segmentId, index) => {
    const segment = segmentsById.get(segmentId)!;
    const duration = Math.max(15, clockToMinutes(segment.endTime) - clockToMinutes(segment.startTime));
    const nextStart = currentStart;
    const nextEnd = nextStart + duration;
    currentStart = nextEnd + (gapsAfterSlot[index] ?? 0);
    return {
      ...segment,
      startTime: minutesToClock(nextStart),
      endTime: minutesToClock(nextEnd)
    };
  });
}

function clockToMinutes(value: string) {
  const [hours, minutes] = value.split(":").map(Number);
  return hours * 60 + minutes;
}

function minutesToClock(value: number) {
  const normalized = ((value % 1440) + 1440) % 1440;
  const hours = Math.floor(normalized / 60);
  const minutes = normalized % 60;
  return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}`;
}

function amapPoiFixture() {
  return {
    id: "B000COFFEE",
    name: "故宫旁咖啡馆",
    type: "餐饮服务;咖啡厅",
    city: "北京市",
    district: "东城区",
    address: "景山前街附近",
    longitude: 116.398,
    latitude: 39.919,
    category: "food",
    source: "amap-place-search",
    sourceNote: "高德 WebService POI 搜索，限定当前城市，extensions=all",
    confidence: 0.91,
    photos: []
  };
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

function nextTick() {
  return new Promise((resolve) => window.setTimeout(resolve, 0));
}

function mockClipboard() {
  const writeText = vi.fn((_text: string) => Promise.resolve());
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText }
  });
  return writeText;
}

function mockDragTargetRect(element: Element, top: number, height = 100) {
  vi.spyOn(element, "getBoundingClientRect").mockReturnValue({
    x: 0,
    y: top,
    top,
    left: 0,
    bottom: top + height,
    right: 100,
    width: 100,
    height,
    toJSON: () => ({})
  } as DOMRect);
}

function fireDragOverAt(element: Element, clientY: number) {
  const event = createEvent.dragOver(element);
  Object.defineProperty(event, "clientY", { configurable: true, value: clientY });
  fireEvent(element, event);
}

function fireDropAt(element: Element, clientY: number) {
  const event = createEvent.drop(element);
  Object.defineProperty(event, "clientY", { configurable: true, value: clientY });
  fireEvent(element, event);
}

function planFixture(): ItineraryPlan {
  return {
    id: "plan_workspace",
    title: "北京地图行程草案",
    city: "北京",
    templateType: "custom",
    budgetEstimate: 40,
    budgetDeltaExplanation: "预算为软约束",
    decisionRationale: "按识别 POI 生成空间顺序",
    status: "draft",
    days: [
      {
        id: "day_1",
        dayNumber: 1,
        title: "历史中轴线与老北京风情",
        weatherSummary: "晴，适合拍照",
        riskSummary: "早高峰可能拥挤",
        totalEstimatedCost: 40,
        segments: [
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
              source: "mock-map-provider",
              confidence: 0.9
            },
            transportMode: "public_transit",
            estimatedCost: 30,
            notes: "深度游览，门票/预约状态待查询"
          },
          {
            id: "seg_2",
            startTime: "12:30",
            endTime: "13:30",
            kind: "activity",
            poi: {
              id: "poi_2",
              name: "景山公园",
              city: "北京",
              category: "park",
              latitude: 39.9236,
              longitude: 116.3969,
              source: "mock-map-provider",
              confidence: 0.74
            },
            transportMode: "public_transit",
            estimatedCost: 10,
            notes: "可作为故宫后续拍照点"
          }
        ]
      }
    ],
    routeOptions: [
      {
        id: "route_1",
        fromSegmentId: "seg_1",
        toSegmentId: "seg_2",
        fromPoiId: "poi_1",
        toPoiId: "poi_2",
        provider: "mock-map-provider",
        mode: "transit",
        label: "公交/地铁",
        isSelected: true,
        sortOrder: 1,
        transportMode: "public_transit",
        distanceMeters: 1900,
        durationSeconds: 1080,
        durationMinutes: 18,
        costAmount: 4,
        costCurrency: "CNY",
        costEstimate: 4,
        crowdingRisk: "medium",
        source: "mock-map-provider",
        polyline: [[116.3972, 39.9163], [116.3969, 39.9236]],
        steps: [],
        providerPayload: {},
        queriedAt: "2026-06-04T00:00:00Z"
      },
      {
        id: "route_2",
        fromSegmentId: "seg_2",
        toSegmentId: "seg_1",
        fromPoiId: "poi_2",
        toPoiId: "poi_1",
        provider: "mock-map-provider",
        mode: "transit",
        label: "公交/地铁",
        isSelected: false,
        sortOrder: 2,
        transportMode: "public_transit",
        distanceMeters: 800,
        durationSeconds: 540,
        durationMinutes: 9,
        costAmount: 4,
        costCurrency: "CNY",
        costEstimate: 4,
        crowdingRisk: "low",
        source: "mock-map-provider",
        polyline: [[116.3969, 39.9236], [116.3972, 39.9163]],
        steps: [],
        providerPayload: {},
        queriedAt: "2026-06-04T00:00:00Z"
      }
    ],
    weatherSignals: [
      {
        id: "weather_1",
        city: "北京",
        date: "2026-06-04",
        dailySummary: "晴，适合拍照",
        hourlyForecast: [],
        riskLevel: "ideal",
        purposeImpactReason: "户外拍照条件较好",
        source: "mock-weather-provider",
        queriedAt: "2026-06-04T00:00:00Z"
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
        queriedAt: "2026-06-04T00:00:00Z"
      }
    ],
    ticketLookupResults: []
  };
}

function threeSegmentPlanFixture(): ItineraryPlan {
  const plan = planFixture();
  return {
    ...plan,
    days: [
      {
        ...plan.days[0],
        totalEstimatedCost: 45,
        segments: [
          ...plan.days[0].segments,
          {
            id: "seg_3",
            startTime: "14:30",
            endTime: "15:30",
            kind: "activity",
            poi: {
              id: "poi_3",
              name: "北海公园",
              city: "北京",
              category: "park",
              latitude: 39.9255,
              longitude: 116.3895,
              source: "mock-map-provider",
              confidence: 0.7
            },
            transportMode: "public_transit",
            estimatedCost: 5,
            notes: "湖边散步，等待 Agent 查询天气和人流风险"
          }
        ]
      }
    ],
    routeOptions: [
      ...plan.routeOptions,
      {
        id: "route_3",
        fromSegmentId: "seg_2",
        toSegmentId: "seg_3",
        fromPoiId: "poi_2",
        toPoiId: "poi_3",
        provider: "mock-map-provider",
        mode: "transit",
        label: "公交/地铁",
        isSelected: true,
        sortOrder: 1,
        transportMode: "public_transit",
        distanceMeters: 1200,
        durationSeconds: 720,
        durationMinutes: 12,
        costAmount: 4,
        costCurrency: "CNY",
        costEstimate: 4,
        crowdingRisk: "low",
        source: "mock-map-provider",
        polyline: [[116.3969, 39.9236], [116.3895, 39.9255]],
        steps: [],
        providerPayload: {},
        queriedAt: "2026-06-04T00:00:00Z"
      }
    ]
  };
}

function planWithAlternativeRouteFixture(): ItineraryPlan {
  const plan = planFixture();
  return {
    ...plan,
    routeOptions: [
      ...plan.routeOptions,
      {
        ...plan.routeOptions[0],
        id: "route_alt",
        label: "打车",
        isSelected: false,
        sortOrder: 2,
        mode: "taxi",
        transportMode: "taxi",
        costAmount: 28,
        costEstimate: 28,
        distanceMeters: 6800,
        durationSeconds: 900,
        durationMinutes: 15
      }
    ]
  };
}

function planWithSecondLegAlternativeRouteFixture(): ItineraryPlan {
  const plan = threeSegmentPlanFixture();
  const secondLeg = plan.routeOptions.find((route) => route.id === "route_3");
  if (!secondLeg) {
    return plan;
  }
  return {
    ...plan,
    routeOptions: [
      ...plan.routeOptions,
      {
        ...secondLeg,
        id: "route_second_alt",
        label: "打车",
        isSelected: false,
        sortOrder: 2,
        mode: "taxi",
        transportMode: "taxi",
        costAmount: 18,
        costEstimate: 18,
        distanceMeters: 2600,
        durationSeconds: 600,
        durationMinutes: 10
      }
    ]
  };
}

function planningRunFixture(runType: string) {
  return {
    id: "run_route",
    runType,
    userInput: "用户确认切换路线方案。",
    preferenceSummary: "用户偏好轻松不赶路，公共交通优先，但长距离可接受打车。",
    itineraryPlanId: "plan_workspace",
    itineraryVersionId: "ver_route",
    understoodRequirements: {
      summary: "目的地：北京；天数：1；用户输入：用户确认切换路线方案。",
      missingFields: [],
      clarificationQuestions: []
    },
    constraintSummary: [{ label: "路线", value: "2 条路线候选" }],
    toolCalls: [
      {
        id: "map-route",
        toolName: "地图/路线能力",
        status: "completed",
        providerName: "amap-route-provider",
        sourceName: "amap-route",
        queriedAt: "2026-06-10T10:00:00Z",
        confidence: 0.82,
        fallbackUsed: false,
        failureReason: null,
        userVisibleCaveat: "",
        summary: "已计算 POI 空间关系、路线距离和交通方式。"
      }
    ],
    sourceAssessments: [],
    feasibilityReport: null,
    finalSummary: "已保存用户确认的路线选择，并重新检查路线、拥挤、票务和天气风险。",
    createdAt: "2026-06-10T10:00:00Z"
  };
}

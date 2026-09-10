import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { AppShell } from "../../src/components/AppShell";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

test("uploaded inspiration shows three comparison templates with collapsible ticket sources", async () => {
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
      return jsonResponse({
        inspirationSetId: "insp_us3",
        status: "extracting",
        sourceMaterialIds: ["mat_text"]
      });
    }
    if (path.endsWith("/inspirations/insp_us3/extract")) {
      return jsonResponse(extractionResponse());
    }
    if (path.endsWith("/itineraries/generate")) {
      return jsonResponse({ plan: itineraryPlan("custom") });
    }
    if (path.endsWith("/itineraries/compare")) {
      return jsonResponse({
        comparisonId: "cmp_1",
        providerName: "bocha-web-search",
        fallbackUsed: false,
        userVisibleCaveat: "票务来源已按可信度排序。",
        plans: [itineraryPlan("low_budget"), itineraryPlan("photo_first"), itineraryPlan("relaxed_pace")]
      });
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);

  fireEvent.change(screen.getByLabelText("Agent 对话文本"), {
    target: { value: "北京 故宫博物院 拍照 预算 3000 元" }
  });
  fireEvent.drop(screen.getByRole("form", { name: "Agent 对话输入" }), {
    dataTransfer: { files: [new File(["image"], "guide.png", { type: "image/png" })], getData: () => "" }
  });
  fireEvent.click(screen.getByText("发送给 Agent"));

  await waitFor(() => expect(screen.getByText("北京 1 日行程")).toBeTruthy());
  fireEvent.click(screen.getByText("行程对比"));

  await waitFor(() => expect(screen.getByText("多个方案比较")).toBeTruthy());
  expect(screen.getByText("低预算")).toBeTruthy();
  expect(screen.getByText("拍照优先")).toBeTruthy();
  expect(screen.getByText("轻松不赶路")).toBeTruthy();
  const sourceSummary = screen.getAllByText("预约来源（3）")[0];
  fireEvent.click(sourceSummary);
  expect(screen.getAllByText("故宫官方预约入口").length).toBeGreaterThan(0);
  expect(screen.getAllByText("聚合购票入口").length).toBeGreaterThan(0);
  expect(screen.getAllByText("公开搜索结果").length).toBeGreaterThan(0);
  expect(screen.getAllByText(/需预约/).length).toBeGreaterThan(0);
  expect(screen.getAllByText(/查询结果仅供参考/).length).toBeGreaterThan(0);
  expect(screen.queryByText(/reservation_required|available|unknown/)).toBeNull();
  expect(screen.queryByText(/Provider：bocha-web-search/)).toBeNull();
  expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith("/source-materials/upload"))).toBe(true);
  const createRequest = fetchMock.mock.calls.find(([url]) => String(url).endsWith("/inspirations"));
  expect(JSON.parse(String(createRequest?.[1]?.body))).toMatchObject({
    socialLinks: [], sourceMaterialIds: ["mat_uploaded"]
  });
});

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" }
  });
}

function extractionResponse() {
  return {
    inspirationSetId: "insp_us3",
    cityCandidates: ["北京"],
    poiCandidates: [{ name: "故宫博物院", confidence: 0.9, sourceLinks: [] }],
    styleTags: ["拍照优先"],
    budgetClues: ["预算 3000 元"],
    routeClues: ["市中心一天"],
    confidence: 0.82,
    needsUserConfirmation: false,
    sourceLinks: [],
    providerName: "mock-vision-provider",
    fallbackUsed: false,
    itineraryDraft: {
      title: "北京灵感行程草案",
      editable: true,
      days: []
    }
  };
}

function itineraryPlan(templateType: string) {
  const labels: Record<string, string> = {
    custom: "北京 1 日行程",
    low_budget: "低预算",
    photo_first: "拍照优先",
    relaxed_pace: "轻松不赶路"
  };
  return {
    id: `plan_${templateType}`,
    title: labels[templateType],
    city: "北京",
    templateType,
    budgetEstimate: templateType === "low_budget" ? 80 : templateType === "relaxed_pace" ? 180 : 120,
    budgetDeltaExplanation: "预算为软约束，当前费用为 demo 估算。",
    decisionRationale: `${labels[templateType]} 决策说明`,
    status: "draft",
    days: [
      {
        id: "day_1",
        dayNumber: 1,
        weatherSummary: "晴，适合拍照",
        riskSummary: "早高峰可能拥挤",
        totalEstimatedCost: 80,
        segments: [
          {
            id: `seg_${templateType}`,
            startTime: "09:30",
            endTime: "11:30",
            kind: "activity",
            poi: {
              id: `poi_${templateType}`,
              name: "故宫博物院",
              city: "北京",
              category: "attraction",
              latitude: 39.9163,
              longitude: 116.3972,
              confidence: 0.9
            },
            transportMode: "public_transit",
            estimatedCost: 30,
            notes: "门票/预约状态待票务查询确认"
          }
        ]
      }
    ],
    routeOptions: [],
    weatherSignals: [],
    trafficCrowdingSignals: [],
    ticketLookupResults: ticketResults(`seg_${templateType}`)
  };
}

function ticketResults(segmentId: string) {
  return [
    {
      id: `${segmentId}_official`,
      segmentId,
      ticketType: "attraction",
      status: "reservation_required",
      priceEstimate: 60,
      bookingUrl: "https://example.com/official",
      sourceName: "故宫官方预约入口",
      sourceUrl: "https://example.com/official",
      credibilityRank: "official",
      queriedAt: "2026-05-31T10:00:00Z",
      caveat: "查询结果仅供参考，请以购票平台为准",
      providerName: "bocha-web-search",
      fallbackUsed: false,
      confidence: 0.82
    },
    {
      id: `${segmentId}_aggregator`,
      segmentId,
      ticketType: "attraction",
      status: "available",
      priceEstimate: 60,
      bookingUrl: "https://example.com/aggregator",
      sourceName: "聚合购票入口",
      sourceUrl: "https://example.com/aggregator",
      credibilityRank: "aggregator",
      queriedAt: "2026-05-31T10:00:00Z",
      caveat: "查询结果仅供参考，请以购票平台为准",
      providerName: "bocha-web-search",
      fallbackUsed: false,
      confidence: 0.72
    },
    {
      id: `${segmentId}_search`,
      segmentId,
      ticketType: "attraction",
      status: "unknown",
      priceEstimate: 60,
      bookingUrl: "https://example.com/search",
      sourceName: "公开搜索结果",
      sourceUrl: "https://example.com/search",
      credibilityRank: "search",
      queriedAt: "2026-05-31T10:00:00Z",
      caveat: "查询结果仅供参考，请以购票平台为准",
      providerName: "bocha-web-search",
      fallbackUsed: false,
      confidence: 0.55
    }
  ];
}

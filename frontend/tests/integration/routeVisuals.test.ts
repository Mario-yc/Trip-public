import { expect, test } from "vitest";
import { type PlannerDay, type RouteOption } from "../../src/services/apiClient";
import { buildRouteLegColorMap, routeLegColor } from "../../src/components/timeline/routeVisuals";

test("route colors are assigned by day order so one day does not reuse a route color family", () => {
  const days = [
    day("day_1", ["seg_1", "seg_2", "seg_3", "seg_4", "seg_5"]),
    day("day_2", ["seg_a", "seg_b", "seg_c"])
  ];
  const dayOneRoutes = [
    route("route_1", "seg_1", "seg_2"),
    route("route_1_alt", "seg_1", "seg_2", false),
    route("route_2", "seg_2", "seg_3"),
    route("route_3", "seg_3", "seg_4"),
    route("route_4", "seg_4", "seg_5")
  ];
  const dayTwoRoutes = [route("route_5", "seg_a", "seg_b"), route("route_6", "seg_b", "seg_c")];
  const colorMap = buildRouteLegColorMap(days, [...dayOneRoutes, ...dayTwoRoutes]);

  const dayOneSelectedColors = [dayOneRoutes[0], dayOneRoutes[2], dayOneRoutes[3], dayOneRoutes[4]].map((item) =>
    routeLegColor(item, colorMap)
  );

  expect(new Set(dayOneSelectedColors).size).toBe(dayOneSelectedColors.length);
  expect(routeLegColor(dayOneRoutes[0], colorMap)).toBe(routeLegColor(dayOneRoutes[1], colorMap));
  expect(routeLegColor(dayTwoRoutes[0], colorMap)).not.toBe(routeLegColor(dayTwoRoutes[1], colorMap));
});

function day(id: string, segmentIds: string[]): PlannerDay {
  return {
    id,
    dayNumber: Number(id.replace(/\D/g, "")) || 1,
    weatherSummary: "",
    riskSummary: "",
    totalEstimatedCost: 0,
    segments: segmentIds.map((segmentId) => ({
      id: segmentId,
      startTime: "09:00",
      endTime: "10:00",
      kind: "visit",
      poi: {
        id: `poi_${segmentId}`,
        name: segmentId,
        city: "北京",
        latitude: 39.9,
        longitude: 116.4,
        source: "test",
        confidence: 1,
        category: "景点"
      },
      transportMode: "transit",
      estimatedCost: 0,
      notes: ""
    }))
  };
}

function route(id: string, fromSegmentId: string, toSegmentId: string, isSelected = true): RouteOption {
  return {
    id,
    fromSegmentId,
    toSegmentId,
    fromPoiId: `poi_${fromSegmentId}`,
    toPoiId: `poi_${toSegmentId}`,
    provider: "test",
    mode: "transit",
    label: "公交/地铁",
    isSelected,
    sortOrder: 1,
    transportMode: "transit",
    distanceMeters: 1000,
    durationSeconds: 600,
    durationMinutes: 10,
    costAmount: 3,
    costCurrency: "CNY",
    costEstimate: 3,
    crowdingRisk: "low",
    source: "test",
    polyline: [],
    steps: [],
    providerPayload: {},
    queriedAt: "2026-07-03T00:00:00.000Z"
  };
}

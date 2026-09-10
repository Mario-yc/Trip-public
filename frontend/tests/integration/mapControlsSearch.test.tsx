import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useEffect, useState } from "react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { AppShell } from "../../src/components/AppShell";
import { PlannerMap } from "../../src/components/map/PlannerMap";
import { plannerStore } from "../../src/state/plannerStore";
import { createComparisonPreviewState } from "../../src/state/planComparisonPreview";

beforeEach(() => {
  resetMapTestStore();
  window.localStorage.removeItem("trip.agentPanelWidth");
  window.localStorage.removeItem("trip.timelinePanelWidth");
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  resetMapTestStore();
});

function resetMapTestStore() {
  plannerStore.setState({
    candidateMapPois: [],
    selectedMapPoi: null,
    poiSelectionStatuses: {},
    pendingPoiCandidates: [],
    activeDensityMapComparison: null,
    agentSession: null,
    itineraryPlan: null,
    activeVersionId: null,
    selectedDayNumber: 1,
    selectedSegmentId: null,
    timelineSelectionRequestId: 0,
    lastPatchError: "",
    comparisonPreview: createComparisonPreviewState()
  });
}

test("map configuration failure shows a user-facing error without internal config names", async () => {
  vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/map/config")) {
      return jsonResponse({ provider: "amap", enabled: false, jsApiKey: "" });
    }
    return jsonResponse({}, 404);
  }));

  render(<PlannerMap plan={null} selectedSegmentId={null} onSelectSegment={() => undefined} city="北京" />);

  await waitFor(() => expect(screen.getByRole("alert")).toBeTruthy());
  expect(screen.getByText("高德地图暂时不可用，请稍后重试。")).toBeTruthy();
  expect(screen.queryByText(/MAP_PROVIDER_KEY|amap-webservice|mock-map-provider/)).toBeNull();
});

test("unknown city does not silently reuse Beijing as the initial map center", async () => {
  const mapOptions: Array<Record<string, unknown>> = [];
  vi.stubGlobal("navigator", { ...navigator, geolocation: undefined });
  vi.stubGlobal("AMap", {
    Map: vi.fn(function Map(_node: HTMLDivElement, options: Record<string, unknown>) {
      mapOptions.push(options);
      return {
        destroy: vi.fn(),
        on: vi.fn(),
        resize: vi.fn(),
        setStatus: vi.fn(),
        setZoomAndCenter: vi.fn()
      };
    }),
    Polyline: vi.fn(function Polyline() {
      return {};
    })
  });
  vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL) => {
    if (String(url).endsWith("/map/config")) {
      return jsonResponse({ provider: "amap", enabled: true, jsApiKey: "web-key" });
    }
    return jsonResponse({}, 404);
  }));

  render(<PlannerMap plan={null} selectedSegmentId={null} onSelectSegment={() => undefined} city="未解析城市" />);

  await waitFor(() => expect(mapOptions).toHaveLength(1));
  expect(mapOptions[0]).not.toHaveProperty("center");
});

test("comparison preview hides every map write affordance until adoption", () => {
  vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ provider: "amap", enabled: false, jsApiKey: "" })));
  render(
    <PlannerMap
      plan={itineraryPlanFixture()}
      selectedSegmentId="seg_existing"
      onSelectSegment={() => undefined}
      city="北京"
      interactionMode="plan_overview_preview"
    />
  );
  expect(screen.getByLabelText("方案地图只读预览")).toBeTruthy();
  expect(screen.queryByRole("textbox", { name: "地图搜索" })).toBeNull();
  expect(screen.queryByRole("button", { name: "删除当前景点" })).toBeNull();
});

test("comparison preview keeps AMap navigation and marker inspection while search and writes stay disabled", async () => {
  const handlers = new Map<string, (event?: Record<string, unknown>) => void>();
  const setStatus = vi.fn();
  let center = { lng: 116.397, lat: 39.918 };
  let zoom = 12;
  const mapApi = {
    destroy: vi.fn(),
    getCenter: vi.fn(() => center),
    getZoom: vi.fn(() => zoom),
    lngLatToContainer: vi.fn(() => ({ x: 360, y: 240 })),
    on: vi.fn((eventName: string, handler: (event?: Record<string, unknown>) => void) => {
      handlers.set(eventName, handler);
    }),
    setCenter: vi.fn(),
    setFitView: vi.fn(),
    setPitch: vi.fn(),
    setRotation: vi.fn(),
    setStatus,
    setZoomAndCenter: vi.fn(),
    zoomIn: vi.fn(),
    zoomOut: vi.fn()
  };
  const debugStates: Array<{
    dragCount: number;
    zoomCount: number;
    moveCount: number;
    center: [number, number] | null;
    zoom: number | null;
  }> = [];
  vi.stubGlobal("AMap", {
    Map: vi.fn(function Map() {
      return mapApi;
    }),
    Polyline: vi.fn(function Polyline() {
      return { setMap: vi.fn() };
    })
  });
  vi.stubGlobal("navigator", { ...navigator, geolocation: undefined });
  vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL) => {
    if (String(url).endsWith("/map/config")) {
      return jsonResponse({ provider: "amap", enabled: true, jsApiKey: "web-key" });
    }
    return jsonResponse({}, 404);
  }));

  render(
    <PlannerMap
      plan={itineraryPlanFixture()}
      selectedSegmentId="seg_existing"
      onSelectSegment={() => undefined}
      city="北京"
      interactionMode="plan_overview_preview"
      onDebugStateChange={(state) => debugStates.push(state)}
    />
  );

  await waitFor(() => expect(setStatus).toHaveBeenCalledWith({
    dragEnable: true,
    zoomEnable: true,
    scrollWheel: true,
    doubleClickZoom: true
  }));
  const constructor = (window as unknown as { AMap: { Map: ReturnType<typeof vi.fn> } }).AMap.Map;
  expect(constructor.mock.calls[0][1]).toMatchObject({
    dragEnable: true,
    zoomEnable: true,
    scrollWheel: true,
    doubleClickZoom: true
  });
  const stage = document.querySelector(".map-stage") as HTMLElement;
  expect(stage.dataset.mapCanNavigate).toBe("true");
  expect(stage.dataset.mapCanSearch).toBe("false");
  expect(stage.dataset.mapCanMutate).toBe("false");
  expect(screen.queryByRole("textbox", { name: "地图搜索" })).toBeNull();

  act(() => {
    handlers.get("dragstart")?.();
    center = { lng: 116.407, lat: 39.928 };
    handlers.get("mapmove")?.();
    handlers.get("dragend")?.();
    handlers.get("zoomstart")?.();
    zoom = 13;
    handlers.get("zoomchange")?.();
    handlers.get("zoomend")?.();
  });
  await waitFor(() => {
    const latest = debugStates[debugStates.length - 1];
    expect(latest?.dragCount).toBeGreaterThan(0);
    expect(latest?.zoomCount).toBeGreaterThan(0);
    expect(latest?.moveCount).toBeGreaterThan(0);
    expect(latest?.center).toEqual([116.407, 39.928]);
    expect(latest?.zoom).toBe(13);
  });

  fireEvent.click(screen.getByRole("button", { name: "选择 故宫博物院" }));
  expect(await screen.findByRole("button", { name: "查看来源" })).toBeTruthy();
  expect(screen.getByText("只读方案预览")).toBeTruthy();
  expect(screen.getByRole("button", { name: "查看来源" })).toBeTruthy();
  expect(screen.queryByText("加入当前 Day")).toBeNull();
});

test("comparison day changes refit the viewport to the newly visible markers", async () => {
  const setZoomAndCenter = vi.fn();
  const mapApi = {
    destroy: vi.fn(),
    getCenter: vi.fn(() => ({ lng: 116.397026, lat: 39.918058 })),
    getZoom: vi.fn(() => 12),
    lngLatToContainer: vi.fn(() => ({ x: 320, y: 240 })),
    on: vi.fn(),
    setPitch: vi.fn(),
    setRotation: vi.fn(),
    setStatus: vi.fn(),
    setZoomAndCenter
  };
  vi.stubGlobal("AMap", {
    Map: vi.fn(function Map() {
      return mapApi;
    }),
    Polyline: vi.fn(function Polyline() {
      return { setMap: vi.fn() };
    })
  });
  vi.stubGlobal("navigator", { ...navigator, geolocation: undefined });
  vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL) => {
    if (String(url).endsWith("/map/config")) {
      return jsonResponse({ provider: "amap", enabled: true, jsApiKey: "web-key" });
    }
    return jsonResponse({}, 404);
  }));
  const firstDayPlan = itineraryPlanFixture();
  const firstDay = firstDayPlan.days[0];
  const plan = {
    ...firstDayPlan,
    days: [
      firstDay,
      {
        ...firstDay,
        id: "day_map_2",
        dayNumber: 2,
        title: "Day 2",
        segments: firstDay.segments.map((segment) => ({
          ...segment,
          id: "seg_day_2",
          poi: {
            ...segment.poi,
            id: "poi_day_2",
            amapId: "amap_day_2",
            name: "第二天地点",
            longitude: 116.2,
            latitude: 40.2
          }
        }))
      }
    ]
  };

  const view = render(
    <PlannerMap
      plan={plan}
      selectedDayNumber={1}
      selectedSegmentId={null}
      onSelectSegment={() => undefined}
      city="北京"
      interactionMode="plan_overview_preview"
    />
  );
  await waitFor(() => expect(setZoomAndCenter).toHaveBeenCalledWith(14, [116.397026, 39.918058], true, 0));

  setZoomAndCenter.mockClear();
  view.rerender(
    <PlannerMap
      plan={plan}
      selectedDayNumber={2}
      selectedSegmentId={null}
      onSelectSegment={() => undefined}
      city="北京"
      interactionMode="plan_overview_preview"
    />
  );

  await waitFor(() => expect(setZoomAndCenter).toHaveBeenCalledWith(14, [116.2, 40.2], true, 0));
  expect(screen.getByRole("button", { name: "选择 第二天地点" })).toBeTruthy();
  expect(screen.queryByRole("button", { name: "选择 故宫博物院" })).toBeNull();
});

test("map runtime initialization limits concurrent config and location slots to ten", async () => {
  const mapApi = {
    destroy: vi.fn(),
    getZoom: vi.fn(() => 12),
    lngLatToContainer: vi.fn(() => ({ x: 320, y: 220 })),
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
  vi.stubGlobal("AMap", {
    Map: vi.fn(function Map() {
      return mapApi;
    }),
    Marker: vi.fn(function Marker() {
      return { setMap: vi.fn(), on: vi.fn() };
    }),
    Polyline: vi.fn(function Polyline() {
      return { setMap: vi.fn(), setOptions: vi.fn() };
    })
  });

  let activeConfigRequests = 0;
  let maxActiveConfigRequests = 0;
  const configResolvers: Array<() => void> = [];
  vi.stubGlobal("fetch", vi.fn((url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/map/config")) {
      activeConfigRequests += 1;
      maxActiveConfigRequests = Math.max(maxActiveConfigRequests, activeConfigRequests);
      return new Promise<Response>((resolve) => {
        configResolvers.push(() => {
          activeConfigRequests -= 1;
          resolve(jsonResponse({ provider: "amap", enabled: true, jsApiKey: "web-key" }));
        });
      });
    }
    return Promise.resolve(jsonResponse({}, 404));
  }));

  render(
    <>
      {Array.from({ length: 11 }, (_, index) => (
        <PlannerMap key={index} plan={null} selectedSegmentId={null} onSelectSegment={() => undefined} city="北京" />
      ))}
    </>
  );

  await waitFor(() => expect(configResolvers).toHaveLength(10));
  expect(maxActiveConfigRequests).toBe(10);

  act(() => {
    configResolvers[0]?.();
  });
  await waitFor(() => expect(configResolvers).toHaveLength(11));
  expect(maxActiveConfigRequests).toBe(10);

  act(() => {
    configResolvers.slice(1).forEach((resolve) => resolve());
  });
  await waitFor(() => expect((window as unknown as { AMap: { Map: ReturnType<typeof vi.fn> } }).AMap.Map).toHaveBeenCalledTimes(11));
});

test("online location shares the ten-slot map runtime limit", async () => {
  const mapApi = {
    destroy: vi.fn(),
    getZoom: vi.fn(() => 12),
    lngLatToContainer: vi.fn(() => ({ x: 320, y: 220 })),
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
  vi.stubGlobal("AMap", {
    Map: vi.fn(function Map() {
      return mapApi;
    }),
    Marker: vi.fn(function Marker() {
      return { setMap: vi.fn(), on: vi.fn() };
    }),
    Polyline: vi.fn(function Polyline() {
      return { setMap: vi.fn(), setOptions: vi.fn() };
    })
  });
  vi.stubGlobal("fetch", vi.fn((url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/map/config")) {
      return Promise.resolve(jsonResponse({ provider: "amap", enabled: true, jsApiKey: "web-key" }));
    }
    return Promise.resolve(jsonResponse({}, 404));
  }));

  let activeLocations = 0;
  let maxActiveLocations = 0;
  const locationResolvers: Array<() => void> = [];
  vi.stubGlobal("navigator", {
    ...navigator,
    geolocation: {
      getCurrentPosition: vi.fn((success: PositionCallback) => {
        activeLocations += 1;
        maxActiveLocations = Math.max(maxActiveLocations, activeLocations);
        locationResolvers.push(() => {
          activeLocations -= 1;
          success({ coords: { longitude: 116.4, latitude: 39.9 } } as GeolocationPosition);
        });
      })
    }
  });

  render(
    <>
      {Array.from({ length: 11 }, (_, index) => (
        <PlannerMap key={index} plan={null} selectedSegmentId={null} onSelectSegment={() => undefined} city="北京" />
      ))}
    </>
  );

  await waitFor(() => expect(locationResolvers).toHaveLength(10));
  expect(maxActiveLocations).toBe(10);

  act(() => {
    locationResolvers[0]?.();
  });
  await waitFor(() => expect(locationResolvers).toHaveLength(11));
  expect(maxActiveLocations).toBe(10);

  act(() => {
    locationResolvers.slice(1).forEach((resolve) => resolve());
  });
  await waitFor(() => expect((window as unknown as { AMap: { Map: ReturnType<typeof vi.fn> } }).AMap.Map).toHaveBeenCalledTimes(11));
});

test("queued map initialization is cancelled on unmount before it can request config", async () => {
  vi.stubGlobal("AMap", {
    Map: vi.fn(function Map() {
      return { destroy: vi.fn(), on: vi.fn() };
    }),
    Marker: vi.fn(function Marker() {
      return { setMap: vi.fn(), on: vi.fn() };
    }),
    Polyline: vi.fn(function Polyline() {
      return { setMap: vi.fn(), setOptions: vi.fn() };
    })
  });

  const configResolvers: Array<() => void> = [];
  const fetchMock = vi.fn((url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/map/config")) {
      return new Promise<Response>((resolve) => {
        configResolvers.push(() => resolve(jsonResponse({ provider: "amap", enabled: true, jsApiKey: "web-key" })));
      });
    }
    return Promise.resolve(jsonResponse({}, 404));
  });
  vi.stubGlobal("fetch", fetchMock);

  const { unmount } = render(
    <>
      {Array.from({ length: 11 }, (_, index) => (
        <PlannerMap key={index} plan={null} selectedSegmentId={null} onSelectSegment={() => undefined} city="北京" />
      ))}
    </>
  );

  await waitFor(() => expect(configResolvers).toHaveLength(10));
  unmount();
  act(() => {
    configResolvers.forEach((resolve) => resolve());
  });

  await new Promise((resolve) => setTimeout(resolve, 0));
  expect(fetchMock.mock.calls.filter((call) => String(call[0]).endsWith("/map/config"))).toHaveLength(10);
});

test("map POI search shares the three-slot basic search service limit", async () => {
  const mapApi = {
    destroy: vi.fn(),
    getZoom: vi.fn(() => 12),
    lngLatToContainer: vi.fn(() => ({ x: 320, y: 220 })),
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
  vi.stubGlobal("AMap", {
    Map: vi.fn(function Map() {
      return mapApi;
    }),
    Marker: vi.fn(function Marker() {
      return { setMap: vi.fn(), on: vi.fn(), setOptions: vi.fn() };
    }),
    Polyline: vi.fn(function Polyline() {
      return { setMap: vi.fn(), setOptions: vi.fn() };
    }),
    Circle: vi.fn(function Circle() {
      return { setMap: vi.fn() };
    })
  });
  vi.stubGlobal("navigator", { ...navigator, geolocation: undefined });

  let activePoiRequests = 0;
  let maxActivePoiRequests = 0;
  const poiResolvers: Array<() => void> = [];
  vi.stubGlobal("fetch", vi.fn((url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/map/config")) {
      return Promise.resolve(jsonResponse({ provider: "amap", enabled: true, jsApiKey: "web-key" }));
    }
    if (path.includes("/map/pois?")) {
      activePoiRequests += 1;
      maxActivePoiRequests = Math.max(maxActivePoiRequests, activePoiRequests);
      const decodedPath = decodeURIComponent(path);
      const name = decodedPath.match(/keyword=([^&]+)/)?.[1] ?? "候选地点";
      return new Promise<Response>((resolve) => {
        poiResolvers.push(() => {
          activePoiRequests -= 1;
          resolve(jsonResponse(poiResponse(name, "scenic")));
        });
      });
    }
    return Promise.resolve(jsonResponse({}, 404));
  }));

  render(<PlannerMap plan={null} selectedSegmentId={null} onSelectSegment={() => undefined} city="北京" />);

  const input = await screen.findByPlaceholderText("搜索景点 / 餐厅 / 体验");
  const searchButton = screen.getByText("搜索");
  for (let index = 0; index < 11; index += 1) {
    fireEvent.change(input, { target: { value: `地点${index}` } });
    fireEvent.click(searchButton);
  }

  await waitFor(() => expect(poiResolvers).toHaveLength(3));
  expect(maxActivePoiRequests).toBe(3);

  act(() => {
    poiResolvers[0]?.();
  });
  await waitFor(() => expect(poiResolvers).toHaveLength(4));
  expect(maxActivePoiRequests).toBe(3);

  let resolvedCount = 1;
  while (poiResolvers.length < 11) {
    const end = poiResolvers.length;
    act(() => {
      poiResolvers.slice(resolvedCount, end).forEach((resolve) => resolve());
    });
    resolvedCount = end;
    await waitFor(() => expect(poiResolvers.length).toBeGreaterThan(end));
  }
  expect(maxActivePoiRequests).toBe(3);
  act(() => {
    poiResolvers.slice(resolvedCount).forEach((resolve) => resolve());
  });
});
test("map POI candidates are isolated when a new agent session starts", async () => {
  vi.stubGlobal("AMap", {
    Map: vi.fn(function Map() {
      return {
        destroy: vi.fn(),
        getZoom: vi.fn(() => 12),
        lngLatToContainer: vi.fn(() => ({ x: 360, y: 260 })),
        on: vi.fn(),
        setFitView: vi.fn(),
        setCenter: vi.fn(),
        zoomIn: vi.fn(),
        zoomOut: vi.fn()
      };
    }),
    Marker: vi.fn(function Marker() {
      return { setMap: vi.fn(), on: vi.fn() };
    }),
    Polyline: vi.fn(function Polyline() {
      return {};
    }),
    Circle: vi.fn(function Circle() {
      return { setMap: vi.fn() };
    })
  });
  vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/map/config")) {
      return jsonResponse({ provider: "amap", enabled: true, jsApiKey: "web-key" });
    }
    if (path.includes("/map/pois/nearby")) {
      return jsonResponse(poiResponse("烟袋斜街", "scenic", 116.39, 39.94));
    }
    if (path.includes("/map/pois")) {
      return jsonResponse(poiResponse("故宫博物院", "scenic"));
    }
    return jsonResponse({}, 404);
  }));

  plannerStore.setState({
    agentSession: {
      sessionId: "sess_old_map",
      status: "active",
      city: "北京",
      title: "旧对话",
      activePlanId: "plan_old_map",
      activeVersionId: null,
      turns: [],
      itinerary: null,
      pendingPoiCandidates: []
    }
  });

  render(<PlannerMap plan={null} selectedSegmentId={null} onSelectSegment={() => undefined} city="北京" />);

  await waitFor(() => expect(screen.getByPlaceholderText("搜索景点 / 餐厅 / 体验")).toBeTruthy());
  fireEvent.change(screen.getByPlaceholderText("搜索景点 / 餐厅 / 体验"), { target: { value: "故宫" } });
  fireEvent.click(screen.getByText("搜索"));
  await waitFor(() => expect(plannerStore.getSnapshot().candidateMapPois).toHaveLength(1));
  expect(screen.getByText("故宫博物院")).toBeTruthy();

  act(() => {
    plannerStore.setState({
      agentSession: {
        sessionId: "sess_isolated_new",
        status: "active",
        city: "北京",
        title: "新对话",
        activePlanId: "plan_isolated",
        activeVersionId: null,
        turns: [],
        itinerary: null,
        pendingPoiCandidates: []
      }
    });
  });

  await waitFor(() => expect(plannerStore.getSnapshot().candidateMapPois).toHaveLength(0));
  expect(plannerStore.getSnapshot().selectedMapPoi).toBeNull();
  expect(screen.queryByText("故宫博物院")).toBeNull();
});

test("map starts empty, ignores empty search, then shows real AMap POIs and photo viewer", async () => {
  const dragHandlers: Array<() => void> = [];
  const moveStartHandlers: Array<() => void> = [];
  const moveEndHandlers: Array<() => void> = [];
  const zoomStartHandlers: Array<() => void> = [];
  const mapClickHandlers: Array<(event?: Record<string, unknown>) => void> = [];
  const circleOptions: Array<Record<string, unknown>> = [];
  const circleOverlays: Array<{ setMap: ReturnType<typeof vi.fn>; setOptions: ReturnType<typeof vi.fn> }> = [];
  const mapApi = {
    destroy: vi.fn(),
    lngLatToContainer: vi.fn(([longitude, latitude]: [number, number]) => ({
      x: longitude === 116.397026 ? 360 : 420,
      y: latitude === 39.918058 ? 260 : 220
    })),
    getZoom: vi.fn(() => 12),
    add: vi.fn(),
    remove: vi.fn(),
    panTo: vi.fn(() => moveStartHandlers[0]?.()),
    on: vi.fn((eventName: string, handler: (event?: Record<string, unknown>) => void) => {
      if (eventName === "dragstart") {
        dragHandlers.push(() => handler());
      }
      if (eventName === "movestart") {
        moveStartHandlers.push(() => handler());
      }
      if (eventName === "moveend") {
        moveEndHandlers.push(() => handler());
      }
      if (eventName === "zoomstart") {
        zoomStartHandlers.push(() => handler());
      }
      if (eventName === "click") {
        mapClickHandlers.push(handler);
      }
    }),
    setFitView: vi.fn(),
    setCenter: vi.fn(),
    zoomIn: vi.fn(),
    zoomOut: vi.fn(),
    setPitch: vi.fn(),
    setRotation: vi.fn(),
    setZoomAndCenter: vi.fn(() => moveStartHandlers[0]?.())
  };
  vi.stubGlobal("AMap", {
    Map: vi.fn(function Map() {
      return mapApi;
    }),
    Marker: vi.fn(function Marker() {
      throw new Error("AMap Marker should not be constructed for map POI pins");
    }),
    Circle: vi.fn(function Circle(options: Record<string, unknown>) {
      const overlay = { setMap: vi.fn(), setOptions: vi.fn() };
      circleOptions.push(options);
      circleOverlays.push(overlay);
      return overlay;
    }),
    Polyline: vi.fn(function Polyline() {
      return {};
    })
  });

  const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    if (path.endsWith("/map/config")) {
      return jsonResponse({ provider: "amap", enabled: true, jsApiKey: "web-key" });
    }
    if (path.endsWith("/agent/sessions") && init?.method === "POST") {
      return jsonResponse({
        sessionId: "sess_map_first",
        status: "active",
        city: "北京",
        title: "北京 AI 行程",
        activePlanId: "plan_map_first",
        activeVersionId: null,
        turns: [],
        itinerary: null,
        pendingPoiCandidates: []
      });
    }
    if (path.endsWith("/map/pois/resolve")) {
      const body = JSON.parse(String(init?.body ?? "{}"));
      expect(body.sessionId).toBe("sess_map_first");
      return jsonResponse({
        resolved: [{ query: "故宫博物院", status: "accepted", poi: poiResponse("故宫博物院", "scenic").pois[0] }],
        pending: []
      });
    }
    if (path.includes("/map/pois/nearby")) {
      const parsed = new URL(path);
      if (parsed.searchParams.get("keyword") === "咖啡") {
        expect(Number(parsed.searchParams.get("longitude"))).toBeCloseTo(116.4102);
        expect(Number(parsed.searchParams.get("latitude"))).toBeCloseTo(39.9201);
        expect(parsed.searchParams.get("radius")).toBe("3000");
        return jsonResponse(poiResponse("附近咖啡店", "food"));
      }
      if (parsed.searchParams.get("keyword") === "北海公园") {
        expect(Number(parsed.searchParams.get("longitude"))).toBeCloseTo(116.389);
        expect(Number(parsed.searchParams.get("latitude"))).toBeCloseTo(39.925);
        return jsonResponse(poiResponse("北海公园", "scenic", 116.389, 39.925, 35));
      }
      expect(Number(parsed.searchParams.get("longitude"))).toBeCloseTo(116.41);
      expect(Number(parsed.searchParams.get("latitude"))).toBeCloseTo(39.92);
      return jsonResponse(poiResponse("天坛公园", "scenic", 116.4102, 39.9201));
    }
    if (path.includes("/map/pois")) {
      const parsed = new URL(path);
      return jsonResponse(poiResponse(parsed.searchParams.get("keyword") ? "故宫博物院" : "北京烤鸭店", parsed.searchParams.get("category") ?? "all"));
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  const { rerender } = render(<PlannerMap plan={null} selectedSegmentId={null} onSelectSegment={() => undefined} city="北京" />);

  await waitFor(() => expect(fetchMock.mock.calls.some((call) => String(call[0]).endsWith("/map/config"))).toBe(true));
  const mapConstructor = (window as unknown as { AMap: { Map: ReturnType<typeof vi.fn> } }).AMap.Map;
  expect(mapConstructor.mock.calls[0][1]).toMatchObject({
    animateEnable: false,
    buildingAnimation: false,
    features: ["bg", "road", "point"],
    jogEnable: false,
    pitch: 0,
    rotation: 0,
    viewMode: "2D",
    showIndoorMap: false
  });
  const registeredEvents = mapApi.on.mock.calls.map((call) => call[0]);
  expect(registeredEvents).toContain("mapmove");
  expect(registeredEvents).toContain("moveend");
  expect(registeredEvents).toContain("zoomchange");
  expect(registeredEvents).toContain("zoomend");
  expect(registeredEvents).toContain("zoomstart");
  expect(screen.queryByText("故宫博物院")).toBeNull();
  expect(screen.queryByText("体验")).toBeNull();
  expect(screen.queryByText("购物")).toBeNull();
  expect(screen.getByText("景点")).toBeTruthy();
  expect(screen.getByText("美食")).toBeTruthy();
  expect(screen.getByText("住宿")).toBeTruthy();
  expect(screen.getByText("交通")).toBeTruthy();

  fireEvent.click(screen.getByText("搜索"));
  expect(fetchMock.mock.calls.some((call) => String(call[0]).includes("/map/pois?"))).toBe(false);

  fireEvent.change(screen.getByPlaceholderText("搜索景点 / 餐厅 / 体验"), { target: { value: "故宫" } });
  fireEvent.click(screen.getByText("搜索"));
  await waitFor(() => expect(screen.getAllByText("故宫博物院").length).toBeGreaterThan(0));
  expect(plannerStore.getSnapshot().candidateMapPois[0].name).toBe("故宫博物院");
  act(() => {
    plannerStore.setState({ selectedMapPoi: plannerStore.getSnapshot().candidateMapPois[0] });
  });
  await waitFor(() =>
    expect(screen.getByLabelText("选择 故宫博物院").className).toContain("selected")
  );
  expect((window as unknown as { AMap: { Marker: ReturnType<typeof vi.fn> } }).AMap.Marker).not.toHaveBeenCalled();
  act(() => {
    plannerStore.setState({ selectedMapPoi: null });
  });
  await waitFor(() => expect(screen.getByLabelText("选择 故宫博物院").className).not.toContain("selected"));
  expect(screen.getByText(/高德返回地址/)).toBeTruthy();
  expect(screen.queryByText("来源说明")).toBeNull();
  expect(screen.queryByText("照片")).toBeNull();
  const candidateImage = screen.getAllByAltText("故宫博物院 1").find((image) => image.closest(".poi-strip-card"));
  expect(candidateImage?.closest(".poi-strip-media")).toBeTruthy();
  rerender(<PlannerMap plan={null} selectedSegmentId={null} onSelectSegment={() => undefined} city="北京" interactionMode="plan_overview_preview" />);
  await waitFor(() => {
    expect(document.querySelector(".map-dom-marker.candidate")).toBeNull();
    expect(screen.queryByLabelText("Recommended POI cards")).toBeNull();
    expect(plannerStore.getSnapshot().candidateMapPois).toEqual([]);
  });
  rerender(<PlannerMap plan={null} selectedSegmentId={null} onSelectSegment={() => undefined} city="北京" />);
  const poiStrip = screen.getByLabelText("Recommended POI cards") as HTMLDivElement;
  Object.defineProperty(poiStrip, "clientWidth", { configurable: true, value: 320 });
  Object.defineProperty(poiStrip, "scrollWidth", { configurable: true, value: 900 });
  poiStrip.scrollLeft = 0;
  fireEvent.wheel(poiStrip, { deltaX: 0, deltaY: 120 });
  expect(poiStrip.scrollLeft).toBe(120);

  fireEvent.keyDown(document, { key: "Escape" });
  await waitFor(() => expect(screen.queryByText("故宫博物院")).toBeNull());
  expect(plannerStore.getSnapshot().candidateMapPois).toEqual([]);
  expect(plannerStore.getSnapshot().selectedMapPoi).toBeNull();

  const searchButton = screen.getByText("搜索");
  const scenicButton = screen.getByText("景点");
  expect(searchButton.compareDocumentPosition(scenicButton) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

  fireEvent.click(screen.getByText("交通"));
  await waitFor(() => {
    const transportCall = fetchMock.mock.calls.find((call) => {
      const url = String(call[0]);
      return url.includes("/map/pois?") && url.includes("keyword=%E4%BA%A4%E9%80%9A");
    });
    expect(transportCall).toBeTruthy();
  });

  fireEvent.click(screen.getByText("住宿"));
  await waitFor(() => {
    const lodgingCall = fetchMock.mock.calls.find((call) => {
      const url = String(call[0]);
      return url.includes("/map/pois?") && url.includes("keyword=%E9%85%92%E5%BA%97");
    });
    expect(lodgingCall).toBeTruthy();
  });

  fireEvent.click(screen.getByText("故宫博物院"));
  expect(mapApi.setCenter).toHaveBeenCalledWith([116.397026, 39.918058], true, 0);
  expect(mapApi.setZoomAndCenter).not.toHaveBeenCalled();
  expect(mapApi.setFitView).not.toHaveBeenCalled();
  await waitFor(() =>
    expect(screen.getByLabelText("选择 故宫博物院").className).toContain("selected")
  );
  expect((window as unknown as { AMap: { Marker: ReturnType<typeof vi.fn> } }).AMap.Marker).not.toHaveBeenCalled();
  expect(screen.getByLabelText("附近搜索")).toBeTruthy();
  zoomStartHandlers[0]?.();
  await waitFor(() => expect(screen.queryByLabelText("附近搜索")).toBeNull());
  fireEvent.click(screen.getAllByText("故宫博物院")[0]);
  expect(screen.getByLabelText("附近搜索")).toBeTruthy();
  expect(plannerStore.getSnapshot().selectedMapPoi?.name).toBe("故宫博物院");
  expect(screen.getByText("加入 Day1")).toBeTruthy();
  expect(screen.getByText("忽略该 POI")).toBeTruthy();
  expect(screen.getByText("查看来源")).toBeTruthy();
  fireEvent.click(screen.getByText("忽略该 POI"));
  expect(plannerStore.getSnapshot().poiSelectionStatuses[plannerStore.getSnapshot().selectedMapPoi?.id ?? ""]).toBe("ignored");
  fireEvent.click(screen.getByText("验证 POI"));
  await waitFor(() => expect(screen.getByText("POI 已通过高德验证：故宫博物院")).toBeTruthy());

  dragHandlers[0]?.();
  await waitFor(() => expect(screen.queryByLabelText("附近搜索")).toBeNull());
  expect(plannerStore.getSnapshot().selectedMapPoi?.name).toBe("故宫博物院");
  moveEndHandlers[0]?.();
  await waitFor(() => expect(plannerStore.getSnapshot().selectedMapPoi).toBeNull());

  fireEvent.click(screen.getByLabelText("选择 故宫博物院"));
  expect(mapApi.setCenter).toHaveBeenCalledWith([116.397026, 39.918058], true, 0);
  expect(mapApi.setZoomAndCenter).not.toHaveBeenCalled();
  expect(mapApi.setFitView).not.toHaveBeenCalled();
  await waitFor(() => expect(screen.getByLabelText("附近搜索")).toBeTruthy());
  expect(screen.getAllByAltText("故宫博物院 1").length).toBeGreaterThan(1);
  expect(screen.getAllByText("故宫博物院").length).toBeGreaterThan(0);
  expect(screen.queryByText(/WebService POI 搜索|extensions=all/)).toBeNull();

  fireEvent.pointerDown(screen.getByText("路线待地点确认后生成"));
  await waitFor(() => expect(screen.queryByLabelText("附近搜索")).toBeNull());

  mapClickHandlers[0]?.({
    poi: {
      name: "天坛公园",
      location: {
        getLng: () => 116.41,
        getLat: () => 39.92
      }
    }
  });
  await waitFor(() => expect(screen.getAllByText("天坛公园").length).toBeGreaterThan(0));
  await waitFor(() => expect(screen.getByLabelText("附近搜索")).toBeTruthy());
  expect(mapApi.setCenter).toHaveBeenCalledWith([116.4102, 39.9201], true, 0);
  expect(mapApi.setZoomAndCenter).not.toHaveBeenCalled();

  fireEvent.click(screen.getAllByAltText("天坛公园 1").find((image) => image.closest(".poi-map-thumbnail"))!.closest("button")!);
  expect(screen.getByRole("dialog", { name: "天坛公园 照片查看器" })).toBeTruthy();
  expect(screen.getByText("来源：高德地图")).toBeTruthy();
  const sourceLink = screen.getByRole("link", { name: /来源网址/ });
  expect(sourceLink.getAttribute("target")).toBe("_blank");
  expect(sourceLink.getAttribute("rel")).toBe("noreferrer");
  fireEvent.click(screen.getByText("›"));
  fireEvent.click(screen.getByText("‹"));

  fireEvent.change(screen.getByLabelText("附近搜索"), { target: { value: "咖啡" } });
  fireEvent.change(screen.getByLabelText("附近搜索范围"), { target: { value: "3000" } });
  fireEvent.click(screen.getByText("查找"));
  await waitFor(() => expect(screen.getAllByText("附近咖啡店").length).toBeGreaterThan(0));
  const nearbyCircleIndex = circleOptions.findIndex(
    (options) => options.radius === 3000 && JSON.stringify(options.center) === JSON.stringify([116.4102, 39.9201])
  );
  expect(nearbyCircleIndex).toBeGreaterThanOrEqual(0);
  fireEvent.keyDown(document, { key: "Escape" });
  await waitFor(() => expect(screen.queryByText("附近咖啡店")).toBeNull());
  expect(mapApi.remove).not.toHaveBeenCalledWith(circleOverlays[nearbyCircleIndex]);
  expect(circleOverlays[nearbyCircleIndex]?.setMap).toHaveBeenCalledWith(null);
  expect(screen.getAllByText("天坛公园").length).toBeGreaterThan(0);

  mapClickHandlers[0]?.({
    pois: [
      {
        name: "北海公园",
        location: {
          getLng: () => 116.389,
          getLat: () => 39.925
        }
      }
    ]
  });
  await waitFor(() => expect(screen.getAllByText("北海公园").length).toBeGreaterThan(0));
  expect(mapApi.setCenter).toHaveBeenCalledWith([116.389, 39.925], true, 0);

  fireEvent.click(screen.getByText("+"));
  fireEvent.click(screen.getByText("−"));
  const fixed2DButton = screen.getByRole("button", { name: "2D" });
  expect(fixed2DButton).toHaveProperty("disabled", true);
  fireEvent.click(fixed2DButton);
  fireEvent.click(screen.getByText("↻"));
  expect(mapApi.zoomIn).toHaveBeenCalled();
  expect(mapApi.zoomOut).toHaveBeenCalled();
  expect(mapApi.setPitch).not.toHaveBeenCalled();
  expect(mapApi.setRotation).not.toHaveBeenCalled();

  dragHandlers[0]?.();
  expect(screen.queryByRole("dialog")).toBeNull();
});

test("clears local candidate POIs when switching agent sessions", async () => {
  plannerStore.setState({
    agentSession: {
      sessionId: "sess_old_map",
      status: "active",
      city: "北京",
      title: "旧对话",
      activePlanId: "plan_old_map",
      activeVersionId: null,
      turns: [],
      itinerary: null,
      pendingPoiCandidates: []
    },
    pendingPoiCandidates: []
  });
  vi.stubGlobal("AMap", {
    Map: vi.fn(function Map() {
      return {
        destroy: vi.fn(),
        lngLatToContainer: vi.fn(() => ({ x: 320, y: 220 })),
        on: vi.fn(),
        panTo: vi.fn(),
        setFitView: vi.fn()
      };
    }),
    Marker: vi.fn(function Marker() {
      return { setMap: vi.fn(), on: vi.fn() };
    }),
    Polyline: vi.fn(function Polyline() {
      return { setMap: vi.fn(), setOptions: vi.fn() };
    })
  });
  const fetchMock = vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/map/config")) {
      return jsonResponse({ provider: "amap", enabled: true, jsApiKey: "web-key" });
    }
    if (path.includes("/map/pois")) {
      return jsonResponse(poiResponse("故宫博物院", "scenic"));
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<PlannerMap plan={null} selectedSegmentId={null} onSelectSegment={() => undefined} city="北京" />);

  fireEvent.change(screen.getByPlaceholderText("搜索景点 / 餐厅 / 体验"), { target: { value: "故宫" } });
  fireEvent.click(screen.getByText("搜索"));
  await waitFor(() => expect(screen.getAllByText("故宫博物院").length).toBeGreaterThan(0));
  expect(plannerStore.getSnapshot().candidateMapPois[0].name).toBe("故宫博物院");

  act(() => {
    plannerStore.setState({
      agentSession: {
        sessionId: "sess_next_map",
        status: "active",
        city: "北京",
        title: "新的对话",
        activePlanId: "plan_next_map",
        activeVersionId: null,
        turns: [],
        itinerary: null,
        pendingPoiCandidates: []
      },
      pendingPoiCandidates: []
    });
  });

  await waitFor(() => expect(plannerStore.getSnapshot().candidateMapPois).toEqual([]));
  expect(plannerStore.getSnapshot().selectedMapPoi).toBeNull();
  expect(screen.queryByText("故宫博物院")).toBeNull();
});

test("workspace panels collapse, resize, and preference note toggles", async () => {
  Object.defineProperty(window, "innerWidth", { configurable: true, value: 1680 });
  Object.defineProperty(HTMLElement.prototype, "setPointerCapture", {
    configurable: true,
    value: vi.fn()
  });
  vi.stubGlobal("AMap", {
    Map: vi.fn(function Map() {
      return { destroy: vi.fn(), on: vi.fn(), setZoomAndCenter: vi.fn() };
    }),
    Marker: vi.fn(function Marker() {
      return { setMap: vi.fn() };
    }),
    Polyline: vi.fn(function Polyline() {
      return {};
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
    return jsonResponse({}, 404);
  }));

  render(<AppShell />);
  const shell = screen.getByRole("main") as HTMLElement;

  const preferenceTrigger = screen.getByRole("button", { name: "旅行偏好卡片" });
  expect(preferenceTrigger.getAttribute("aria-expanded")).toBe("false");
  expect(screen.queryByLabelText("旅行偏好详情")).toBeNull();
  fireEvent.click(preferenceTrigger);
  expect(screen.getByLabelText("旅行偏好详情")).toBeTruthy();
  expect(preferenceTrigger.getAttribute("aria-expanded")).toBe("true");
  fireEvent.click(preferenceTrigger);
  expect(screen.queryByLabelText("旅行偏好详情")).toBeNull();
  expect(preferenceTrigger.getAttribute("aria-expanded")).toBe("false");

  const leftResizer = screen.getByLabelText("调整左侧对话宽度");
  fireEvent.pointerDown(leftResizer, { pointerId: 1, clientX: 310 });
  const leftResizeMove = new Event("pointermove", { bubbles: true });
  Object.defineProperty(leftResizeMove, "clientX", { value: 420 });
  fireEvent(leftResizer, leftResizeMove);
  fireEvent.pointerUp(leftResizer, { pointerId: 1 });
  expect(shell.style.getPropertyValue("--agent-panel-width")).toBe("420px");

  fireEvent.click(screen.getByLabelText("收缩左侧对话"));
  expect(screen.getByLabelText("展开左侧对话")).toBeTruthy();
  fireEvent.click(screen.getByLabelText("收缩右侧时间轴"));
  expect(screen.getByLabelText("展开右侧时间轴")).toBeTruthy();
});

test.each([1366, 1440, 1680])("workspace defaults retain a usable map at %ipx viewport", async (viewportWidth) => {
  Object.defineProperty(window, "innerWidth", { configurable: true, value: viewportWidth });
  window.localStorage.setItem("trip.agentPanelWidth", "370");
  window.localStorage.setItem("trip.timelinePanelWidth", "530");
  vi.stubGlobal("AMap", {
    Map: vi.fn(function Map() { return { destroy: vi.fn(), on: vi.fn(), setZoomAndCenter: vi.fn() }; }),
    Marker: vi.fn(function Marker() { return { setMap: vi.fn() }; }),
    Polyline: vi.fn(function Polyline() { return {}; })
  });
  vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) return jsonResponse({ mode: "mock", default: [], mock: [] });
    if (path.endsWith("/map/config")) return jsonResponse({ provider: "amap", enabled: true, jsApiKey: "web-key" });
    return jsonResponse({}, 404);
  }));

  render(<AppShell />);
  const shell = screen.getByRole("main") as HTMLElement;
  expect(shell.style.getPropertyValue("--agent-panel-width")).toBe("370px");
  expect(shell.style.getPropertyValue("--timeline-panel-width")).toBe("530px");
  expect(370 + 530 + 12 + 420).toBeLessThanOrEqual(viewportWidth);
});

test("base map POI lookup surfaces AMap failure without showing mock places", async () => {
  const mapClickHandlers: Array<(event?: Record<string, unknown>) => void> = [];
  vi.stubGlobal("AMap", {
    Map: vi.fn(function Map() {
      return {
        destroy: vi.fn(),
        getZoom: vi.fn(() => 12),
        lngLatToContainer: vi.fn(() => ({ x: 320, y: 240 })),
        on: vi.fn((eventName: string, handler: (event?: Record<string, unknown>) => void) => {
          if (eventName === "click") {
            mapClickHandlers.push(handler);
          }
        }),
        setZoomAndCenter: vi.fn()
      };
    }),
    Marker: vi.fn(function Marker() {
      return { setMap: vi.fn(), on: vi.fn() };
    }),
    Polyline: vi.fn(function Polyline() {
      return {};
    })
  });
  vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/map/config")) {
      return jsonResponse({ provider: "amap", enabled: true, jsApiKey: "web-key" });
    }
    if (path.includes("/map/pois/nearby")) {
      return jsonResponse({ detail: "AMap nearby POI search failed: INVALID_USER_KEY" }, 502);
    }
    return jsonResponse({}, 404);
  }));

  render(<PlannerMap plan={null} selectedSegmentId={null} onSelectSegment={() => undefined} city="北京" />);
  await waitFor(() => expect(mapClickHandlers.length).toBeGreaterThan(0));

  mapClickHandlers[0]?.({
    poi: {
      name: "无法查询的地点",
      location: {
        getLng: () => 116.41,
        getLat: () => 39.92
      }
    }
  });

  await waitFor(() => expect(screen.getByText("AMap nearby POI search failed: INVALID_USER_KEY")).toBeTruthy());
  expect(screen.queryByLabelText("附近搜索")).toBeNull();
});

test("POI resolve pending and provider errors are visible from the map", async () => {
  vi.stubGlobal("AMap", {
    Map: vi.fn(function Map() {
      return {
        destroy: vi.fn(),
        getZoom: vi.fn(() => 12),
        lngLatToContainer: vi.fn(() => ({ x: 320, y: 240 })),
        on: vi.fn(),
        setZoomAndCenter: vi.fn()
      };
    }),
    Marker: vi.fn(function Marker() {
      return { setMap: vi.fn(), on: vi.fn() };
    }),
    Polyline: vi.fn(function Polyline() {
      return {};
    })
  });

  let resolveMode: "pending" | "error" = "pending";
  vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    if (path.endsWith("/map/config")) {
      return jsonResponse({ provider: "amap", enabled: true, jsApiKey: "web-key" });
    }
    if (path.endsWith("/agent/sessions") && init?.method === "POST") {
      return jsonResponse({
        sessionId: "sess_map_pending",
        status: "active",
        city: "北京",
        title: "北京 AI 行程",
        activePlanId: "plan_map_pending",
        activeVersionId: null,
        turns: [],
        itinerary: null,
        pendingPoiCandidates: []
      });
    }
    if (path.endsWith("/map/pois/resolve")) {
      const body = JSON.parse(String(init?.body ?? "{}"));
      expect(body.sessionId).toBe("sess_map_pending");
      if (resolveMode === "error") {
        return jsonResponse({ detail: "AMap POI search failed: INVALID_USER_KEY" }, 502);
      }
      return jsonResponse({
        resolved: [],
        pending: [
          {
            candidateRecordId: "cand_pending",
            query: "老胡同餐厅",
            reason: "multiple_candidates",
            candidates: poiResponse("老胡同餐厅", "food").pois
          }
        ]
      });
    }
    if (path.includes("/map/pois")) {
      return jsonResponse(poiResponse("老胡同餐厅", "food"));
    }
    return jsonResponse({}, 404);
  }));

  render(<PlannerMap plan={null} selectedSegmentId={null} onSelectSegment={() => undefined} city="北京" />);
  fireEvent.change(await screen.findByPlaceholderText("搜索景点 / 餐厅 / 体验"), { target: { value: "老胡同餐厅" } });
  fireEvent.click(screen.getByText("搜索"));
  await waitFor(() => expect(screen.getAllByText("老胡同餐厅").length).toBeGreaterThan(0));

  fireEvent.click(screen.getByText("老胡同餐厅"));
  fireEvent.click(screen.getByText("验证 POI"));
  await waitFor(() => expect(screen.getByText("需要确认 POI：multiple_candidates")).toBeTruthy());
  expect(plannerStore.getSnapshot().pendingPoiCandidates[0].id).toBe("cand_pending");

  resolveMode = "error";
  fireEvent.click(screen.getByText("验证 POI"));
  await waitFor(() => expect(screen.getByText("AMap POI search failed: INVALID_USER_KEY")).toBeTruthy());
});

test("same POI click folds and restores the popover without clearing nearby state", async () => {
  const circleOptions: Array<Record<string, unknown>> = [];
  const circleOverlays: Array<{ setMap: ReturnType<typeof vi.fn>; setOptions: ReturnType<typeof vi.fn> }> = [];
  const mapApi = {
    destroy: vi.fn(),
    getZoom: vi.fn(() => 12),
    lngLatToContainer: vi.fn(() => ({ x: 320, y: 240 })),
    on: vi.fn(),
    setCenter: vi.fn(),
    setFitView: vi.fn(),
    setZoomAndCenter: vi.fn()
  };
  vi.stubGlobal("AMap", {
    Map: vi.fn(function Map() {
      return mapApi;
    }),
    Marker: vi.fn(function Marker() {
      throw new Error("AMap Marker should not be constructed for map POI pins");
    }),
    Circle: vi.fn(function Circle(options: Record<string, unknown>) {
      const overlay = { setMap: vi.fn(), setOptions: vi.fn() };
      circleOptions.push(options);
      circleOverlays.push(overlay);
      return overlay;
    }),
    Polyline: vi.fn(function Polyline() {
      return {};
    })
  });

  const fetchMock = vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/map/config")) {
      return jsonResponse({ provider: "amap", enabled: true, jsApiKey: "web-key" });
    }
    if (path.includes("/map/pois/nearby")) {
      const parsed = new URL(path);
      expect(parsed.searchParams.get("keyword")).toBe("咖啡");
      expect(parsed.searchParams.get("radius")).toBe("3000");
      return jsonResponse(poiResponse("附近咖啡店", "food", 116.4102, 39.9201, 140));
    }
    if (path.includes("/map/pois")) {
      return jsonResponse(poiResponse("天坛公园", "scenic", 116.4102, 39.9201));
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  const { container } = render(<PlannerMap plan={null} selectedSegmentId={null} onSelectSegment={() => undefined} city="北京" />);

  fireEvent.change(await screen.findByPlaceholderText("搜索景点 / 餐厅 / 体验"), { target: { value: "天坛" } });
  fireEvent.click(screen.getByText("搜索"));
  await waitFor(() => expect(screen.getAllByText("天坛公园").length).toBeGreaterThan(0));

  await waitFor(() => expect(screen.getByLabelText("选择 天坛公园")).toBeTruthy());
  fireEvent.click(screen.getByLabelText("选择 天坛公园"));
  await waitFor(() => expect(screen.getByLabelText("附近搜索")).toBeTruthy());
  const nearbyCallsBeforeFocus = fetchMock.mock.calls.filter((call) => String(call[0]).includes("/map/pois/nearby")).length;
  fireEvent.focus(screen.getByLabelText("附近搜索"));
  await waitFor(() => expect(circleOptions).toContainEqual(expect.objectContaining({ radius: 1500 })));
  expect(fetchMock.mock.calls.filter((call) => String(call[0]).includes("/map/pois/nearby"))).toHaveLength(nearbyCallsBeforeFocus);

  fireEvent.change(screen.getByLabelText("附近搜索范围"), { target: { value: "3000" } });
  await waitFor(() => expect(circleOptions).toContainEqual(expect.objectContaining({ radius: 3000 })));
  fireEvent.change(screen.getByLabelText("附近搜索"), { target: { value: "咖啡" } });
  fireEvent.click(screen.getByText("查找"));
  await waitFor(() => expect(screen.getAllByText("附近咖啡店").length).toBeGreaterThan(0));
  const activeCircle = circleOverlays[circleOverlays.length - 1];

  fireEvent.click(screen.getAllByText("附近咖啡店").find((item) => item.closest(".poi-strip-card"))!.closest("button")!);
  await waitFor(() => expect(screen.queryByLabelText("附近搜索")).toBeNull());
  expect(container.querySelector(".poi-strip-card.selected")?.textContent).toContain("附近咖啡店");
  expect(screen.getAllByText("附近咖啡店").length).toBeGreaterThan(0);
  expect(activeCircle?.setMap).not.toHaveBeenCalledWith(null);

  fireEvent.click(screen.getAllByText("附近咖啡店").find((item) => item.closest(".poi-strip-card"))!.closest("button")!);
  await waitFor(() => expect(screen.getByLabelText("附近搜索")).toBeTruthy());
  expect((screen.getByLabelText("附近搜索") as HTMLInputElement).value).toBe("咖啡");
  expect((screen.getByLabelText("附近搜索范围") as HTMLSelectElement).value).toBe("3000");
});

test("selected timeline POI supports nearby search and draws the selected radius circle", async () => {
  const circleOptions: Array<Record<string, unknown>> = [];
  const circleOverlays: Array<{ setMap: ReturnType<typeof vi.fn>; setOptions: ReturnType<typeof vi.fn> }> = [];
  const mapApi = {
    destroy: vi.fn(),
    getZoom: vi.fn(() => 12),
    lngLatToContainer: vi.fn(() => ({ x: 320, y: 240 })),
    on: vi.fn(),
    setCenter: vi.fn(),
    setFitView: vi.fn(),
    setZoomAndCenter: vi.fn()
  };
  vi.stubGlobal("AMap", {
    Map: vi.fn(function Map() {
      return mapApi;
    }),
    Marker: vi.fn(function Marker() {
      throw new Error("AMap Marker should not be constructed for map POI pins");
    }),
    Circle: vi.fn(function Circle(options: Record<string, unknown>) {
      const overlay = { setMap: vi.fn(), setOptions: vi.fn() };
      circleOptions.push(options);
      circleOverlays.push(overlay);
      return overlay;
    }),
    Polyline: vi.fn(function Polyline() {
      return {};
    })
  });

  const existingPlan = itineraryPlanFixture();
  const fetchMock = vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/map/config")) {
      return jsonResponse({ provider: "amap", enabled: true, jsApiKey: "web-key" });
    }
    if (path.includes("/map/pois/nearby")) {
      const parsed = new URL(path);
      expect(parsed.searchParams.get("keyword")).toBe("咖啡");
      expect(parsed.searchParams.get("radius")).toBe("1000");
      expect(Number(parsed.searchParams.get("longitude"))).toBeCloseTo(116.397026);
      expect(Number(parsed.searchParams.get("latitude"))).toBeCloseTo(39.918058);
      return jsonResponse(poiResponse("故宫附近咖啡", "food", 116.398, 39.9188, 120));
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<PlannerMap plan={existingPlan} selectedSegmentId="seg_existing" onSelectSegment={() => undefined} city="北京" />);

  await waitFor(() => expect(screen.getByLabelText("附近搜索")).toBeTruthy());
  expect(screen.getByText("当前选中：故宫博物院")).toBeTruthy();
  expect(screen.getByText("查看来源")).toBeTruthy();
  expect(screen.queryByText("加入当前 Day")).toBeNull();
  expect(screen.queryByText("替换当前地点")).toBeNull();
  expect(screen.queryByText("验证 POI")).toBeNull();

  fireEvent.pointerDown(screen.getByText("路线待地点确认后生成"));
  await waitFor(() => expect(screen.queryByLabelText("附近搜索")).toBeNull());
  expect(screen.queryByLabelText("附近搜索")).toBeNull();

  mapApi.setCenter.mockClear();
  act(() => {
    plannerStore.setState({
      selectedSegmentId: "seg_existing",
      timelineSelectionRequestId: plannerStore.getSnapshot().timelineSelectionRequestId + 1,
      selectedMapPoi: null
    });
  });
  await waitFor(() => expect(screen.getByLabelText("附近搜索")).toBeTruthy());
  expect(mapApi.setCenter).toHaveBeenCalledWith([116.397026, 39.918058], true, 0);

  fireEvent.pointerDown(screen.getByText("路线待地点确认后生成"));
  await waitFor(() => expect(screen.queryByLabelText("附近搜索")).toBeNull());

  fireEvent.click(screen.getByLabelText("选择 故宫博物院"));
  await waitFor(() => expect(screen.getByLabelText("附近搜索")).toBeTruthy());

  fireEvent.change(screen.getByLabelText("附近搜索"), { target: { value: "咖啡" } });
  fireEvent.change(screen.getByLabelText("附近搜索范围"), { target: { value: "1000" } });
  fireEvent.click(screen.getByText("查找"));

  await waitFor(() => expect(screen.getAllByText("故宫附近咖啡").length).toBeGreaterThan(0));
  expect(circleOptions).toContainEqual(expect.objectContaining({
    center: [116.397026, 39.918058],
    radius: 1000,
    strokeStyle: "solid"
  }));
  expect(circleOverlays[circleOverlays.length - 1]?.setMap).not.toHaveBeenCalledWith(null);
});

test("nearby search keeps the keyword and expands to the next radius when the current radius has no results", async () => {
  const circleOptions: Array<Record<string, unknown>> = [];
  const mapApi = {
    destroy: vi.fn(),
    getZoom: vi.fn(() => 12),
    lngLatToContainer: vi.fn(() => ({ x: 320, y: 240 })),
    on: vi.fn(),
    setCenter: vi.fn(),
    setFitView: vi.fn(),
    setZoomAndCenter: vi.fn()
  };
  vi.stubGlobal("AMap", {
    Map: vi.fn(function Map() {
      return mapApi;
    }),
    Marker: vi.fn(function Marker() {
      throw new Error("AMap Marker should not be constructed for map POI pins");
    }),
    Circle: vi.fn(function Circle(options: Record<string, unknown>) {
      circleOptions.push(options);
      return { setMap: vi.fn(), setOptions: vi.fn() };
    }),
    Polyline: vi.fn(function Polyline() {
      return {};
    })
  });

  const nearbyRadii: string[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/map/config")) {
      return jsonResponse({ provider: "amap", enabled: true, jsApiKey: "web-key" });
    }
    if (path.includes("/map/pois/nearby")) {
      const parsed = new URL(path);
      const radius = parsed.searchParams.get("radius") ?? "";
      nearbyRadii.push(radius);
      expect(parsed.searchParams.get("keyword")).toBe("咖啡");
      if (radius === "1500") {
        return jsonResponse(poiResponse("扩大范围咖啡店", "food", 116.398, 39.9188, 920));
      }
      return jsonResponse({ ...poiResponse("无结果", "food"), pois: [] });
    }
    return jsonResponse({}, 404);
  }));

  render(<PlannerMap plan={itineraryPlanFixture()} selectedSegmentId="seg_existing" onSelectSegment={() => undefined} city="北京" />);

  await waitFor(() => expect(screen.getByLabelText("附近搜索")).toBeTruthy());
  fireEvent.change(screen.getByLabelText("附近搜索"), { target: { value: "咖啡" } });
  fireEvent.change(screen.getByLabelText("附近搜索范围"), { target: { value: "500" } });
  expect((screen.getByLabelText("附近搜索") as HTMLInputElement).value).toBe("咖啡");

  fireEvent.click(screen.getByText("查找"));

  await waitFor(() => expect(screen.getAllByText("扩大范围咖啡店").length).toBeGreaterThan(0));
  expect(nearbyRadii).toEqual(["500", "1000", "1500"]);
  expect((screen.getByLabelText("附近搜索") as HTMLInputElement).value).toBe("咖啡");
  expect((screen.getByLabelText("附近搜索范围") as HTMLSelectElement).value).toBe("1500");
  expect(plannerStore.getSnapshot().selectedMapPoi?.name).toBe("扩大范围咖啡店");
  expect(circleOptions).toContainEqual(expect.objectContaining({ radius: 1500 }));
});

test("nearby search reports no results after expanding through the largest radius", async () => {
  const circleOptions: Array<Record<string, unknown>> = [];
  const mapApi = {
    destroy: vi.fn(),
    getZoom: vi.fn(() => 12),
    lngLatToContainer: vi.fn(() => ({ x: 320, y: 240 })),
    on: vi.fn(),
    setCenter: vi.fn(),
    setFitView: vi.fn(),
    setZoomAndCenter: vi.fn()
  };
  vi.stubGlobal("AMap", {
    Map: vi.fn(function Map() {
      return mapApi;
    }),
    Marker: vi.fn(function Marker() {
      throw new Error("AMap Marker should not be constructed for map POI pins");
    }),
    Circle: vi.fn(function Circle(options: Record<string, unknown>) {
      circleOptions.push(options);
      return { setMap: vi.fn(), setOptions: vi.fn() };
    }),
    Polyline: vi.fn(function Polyline() {
      return {};
    })
  });

  const nearbyRadii: string[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/map/config")) {
      return jsonResponse({ provider: "amap", enabled: true, jsApiKey: "web-key" });
    }
    if (path.includes("/map/pois/nearby")) {
      const parsed = new URL(path);
      nearbyRadii.push(parsed.searchParams.get("radius") ?? "");
      expect(parsed.searchParams.get("keyword")).toBe("露营");
      return jsonResponse({ ...poiResponse("无结果", "scenic"), pois: [] });
    }
    return jsonResponse({}, 404);
  }));

  render(<PlannerMap plan={itineraryPlanFixture()} selectedSegmentId="seg_existing" onSelectSegment={() => undefined} city="北京" />);

  await waitFor(() => expect(screen.getByLabelText("附近搜索")).toBeTruthy());
  fireEvent.change(screen.getByLabelText("附近搜索"), { target: { value: "露营" } });
  fireEvent.change(screen.getByLabelText("附近搜索范围"), { target: { value: "3000" } });
  fireEvent.click(screen.getByText("查找"));

  await waitFor(() => expect(screen.getByText("在 5km 范围内未找到“露营”，请调整关键词或直接搜索其它地点。")).toBeTruthy());
  expect(nearbyRadii).toEqual(["3000", "5000"]);
  expect((screen.getByLabelText("附近搜索") as HTMLInputElement).value).toBe("露营");
  expect((screen.getByLabelText("附近搜索范围") as HTMLSelectElement).value).toBe("5000");
  expect(circleOptions).toContainEqual(expect.objectContaining({ radius: 5000 }));
});

test("nearby radius circle attach failure does not crash the map", async () => {
  const consoleWarn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
  const circleOptions: Array<Record<string, unknown>> = [];
  const mapApi = {
    destroy: vi.fn(),
    getZoom: vi.fn(() => 12),
    lngLatToContainer: vi.fn(() => ({ x: 320, y: 240 })),
    on: vi.fn(),
    setCenter: vi.fn(),
    setFitView: vi.fn(),
    setZoomAndCenter: vi.fn()
  };
  vi.stubGlobal("AMap", {
    Map: vi.fn(function Map() {
      return mapApi;
    }),
    Marker: vi.fn(function Marker() {
      throw new Error("AMap Marker should not be constructed for map POI pins");
    }),
    Circle: vi.fn(function Circle(options: Record<string, unknown>) {
      circleOptions.push(options);
      return {
        setMap: vi.fn(() => {
          throw new Error("Cannot read properties of undefined (reading 'add')");
        }),
        setOptions: vi.fn()
      };
    }),
    Polyline: vi.fn(function Polyline() {
      return { setMap: vi.fn(), setOptions: vi.fn() };
    })
  });
  vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/map/config")) {
      return jsonResponse({ provider: "amap", enabled: true, jsApiKey: "web-key" });
    }
    return jsonResponse({}, 404);
  }));

  render(<PlannerMap plan={itineraryPlanFixture()} selectedSegmentId="seg_existing" onSelectSegment={() => undefined} city="北京" />);

  await waitFor(() => expect(screen.getByLabelText("附近搜索")).toBeTruthy());
  fireEvent.focus(screen.getByLabelText("附近搜索"));

  await waitFor(() => expect(screen.getAllByText("范围圈暂时无法显示，附近搜索结果仍可查看。").length).toBeGreaterThan(0));
  expect(circleOptions[0]).toEqual(expect.objectContaining({ center: [116.397026, 39.918058], radius: 1500 }));
  expect(circleOptions[0]).not.toHaveProperty("map");
  expect(consoleWarn).toHaveBeenCalledWith("AMap Circle attach failed", expect.any(Error));
});

test("selected AMap POI can be added to the current itinerary day from the map", async () => {
  vi.stubGlobal("AMap", {
    Map: vi.fn(function Map() {
      return {
        destroy: vi.fn(),
        getZoom: vi.fn(() => 12),
        lngLatToContainer: vi.fn(() => ({ x: 320, y: 240 })),
        on: vi.fn(),
        setFitView: vi.fn(),
        setZoomAndCenter: vi.fn()
      };
    }),
    Marker: vi.fn(function Marker() {
      return { setMap: vi.fn(), on: vi.fn() };
    }),
    Polyline: vi.fn(function Polyline() {
      return {};
    })
  });

  const existingPlan = itineraryPlanFixture();
  plannerStore.setState({
    itineraryPlan: existingPlan,
    activeVersionId: "ver_map_1",
    selectedDayNumber: 1,
    selectedSegmentId: "seg_existing"
  });

  const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    if (path.endsWith("/map/config")) {
      return jsonResponse({ provider: "amap", enabled: true, jsApiKey: "web-key" });
    }
    if (path.includes("/map/pois")) {
      return jsonResponse(poiResponse("颐和园", "scenic", 116.2755, 39.9999));
    }
    if (path.endsWith("/itineraries/plan_map_add/patch") && init?.method === "POST") {
      const body = JSON.parse(String(init.body ?? "{}"));
      expect(body.baseVersionId).toBe("ver_map_1");
      expect(body.operations[0]).toMatchObject({
        op: "add_segment",
        dayId: "day_map_1",
        title: "颐和园"
      });
      expect(body.operations[0].amapPoi.id).toBe("poi_颐和园");
      expect(body.operations[0].amapPoi.longitude).toBe(116.2755);
      expect(body.operations[0].amapPoi.latitude).toBe(39.9999);
      return jsonResponse({
        itinerary: {
          ...existingPlan,
          days: [
            {
              ...existingPlan.days[0],
              segments: [
                ...existingPlan.days[0].segments,
                {
                  id: "seg_added_map",
                  dayId: "day_map_1",
                  startTime: "10:00",
                  endTime: "10:30",
                  durationMinutes: 30,
                  transportMode: "walk",
                  estimatedCost: 0,
                  walkingDistanceMeters: 0,
                  notes: "高德 WebService POI 搜索，限定当前城市，extensions=all",
                  poi: {
                    ...poiResponse("颐和园", "scenic", 116.2755, 39.9999).pois[0],
                    amapId: "poi_颐和园"
                  }
                }
              ]
            }
          ]
        },
        patch: { id: "patch_map_add", validationStatus: "accepted" },
        version: { id: "ver_map_2", versionNumber: 2, sourceType: "manual" },
        pendingPoiCandidates: [],
        validationErrors: []
      });
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<PlannerMap plan={existingPlan} selectedSegmentId="seg_existing" onSelectSegment={() => undefined} city="北京" />);

  fireEvent.change(await screen.findByPlaceholderText("搜索景点 / 餐厅 / 体验"), { target: { value: "颐和园" } });
  fireEvent.click(screen.getByText("搜索"));
  await waitFor(() => expect(screen.getAllByText("颐和园").length).toBeGreaterThan(0));

  fireEvent.click(screen.getAllByRole("button", { name: /颐和园/ })[0]);
  expect(plannerStore.getSnapshot().selectedMapPoi?.name).toBe("颐和园");
  fireEvent.click(screen.getByText("加入当前 Day"));

  await waitFor(() => expect(screen.getAllByText("已加入 Day 1：颐和园").length).toBeGreaterThan(0));
  const snapshot = plannerStore.getSnapshot();
  expect(snapshot.activeVersionId).toBe("ver_map_2");
  expect(snapshot.selectedSegmentId).toBe("seg_added_map");
  const addedSegment = snapshot.itineraryPlan?.days[0].segments[snapshot.itineraryPlan.days[0].segments.length - 1];
  expect(addedSegment?.poi.amapId).toBe("poi_颐和园");
  expect(addedSegment?.poi.longitude).toBe(116.2755);
});

test("selected timeline segment can be replaced by the selected map POI", async () => {
  const mapApi = {
    destroy: vi.fn(),
    getZoom: vi.fn(() => 13),
    lngLatToContainer: vi.fn(() => ({ x: 320, y: 240 })),
    on: vi.fn(),
    add: vi.fn(),
    remove: vi.fn(),
    panTo: vi.fn(),
    setCenter: vi.fn(),
    setFitView: vi.fn(),
    setZoomAndCenter: vi.fn()
  };
  vi.stubGlobal("AMap", {
    Map: vi.fn(function Map() {
      return mapApi;
    }),
    Marker: vi.fn(function Marker() {
      return { setMap: vi.fn(), on: vi.fn() };
    }),
    Polyline: vi.fn(function Polyline() {
      return {};
    }),
    Circle: vi.fn(function Circle() {
      return { setMap: vi.fn() };
    })
  });

  const existingPlan = {
    ...itineraryPlanFixture(),
    days: [
      {
        ...itineraryPlanFixture().days[0],
        segments: [
          ...itineraryPlanFixture().days[0].segments,
          {
            id: "seg_second",
            startTime: "10:00",
            endTime: "11:00",
            kind: "activity",
            durationMinutes: 60,
            transportMode: "walk",
            estimatedCost: 0,
            notes: "",
            poi: poiResponse("颐和园", "scenic", 116.2755, 39.9999).pois[0]
          }
        ]
      }
    ]
  };
  plannerStore.setState({
    itineraryPlan: existingPlan,
    activeVersionId: "ver_replace_1",
    selectedDayNumber: 1,
    selectedSegmentId: "seg_existing"
  });
  function ReplacementMapHarness() {
    const [selectedSegmentId, setSelectedSegmentId] = useState("seg_existing");
    return <PlannerMap plan={existingPlan} selectedSegmentId={selectedSegmentId} onSelectSegment={setSelectedSegmentId} city="北京" />;
  }

  const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    if (path.endsWith("/map/config")) {
      return jsonResponse({ provider: "amap", enabled: true, jsApiKey: "web-key" });
    }
    if (path.includes("/map/pois")) {
      return jsonResponse(poiResponse("天坛公园", "scenic", 116.4102, 39.9201));
    }
    if (path.endsWith("/itineraries/plan_map_add/patch") && init?.method === "POST") {
      const body = JSON.parse(String(init.body ?? "{}"));
      expect(body.baseVersionId).toBe("ver_replace_1");
      expect(body.operations[0]).toMatchObject({
        op: "replace_segment_poi",
        segmentId: "seg_existing"
      });
      expect(body.operations[0].amapPoi.name).toBe("天坛公园");
      return jsonResponse({
        itinerary: {
          ...existingPlan,
          days: [
            {
              ...existingPlan.days[0],
              segments: [
                {
                  ...existingPlan.days[0].segments[0],
                  notes: "已替换为高德地点",
                  poi: {
                    ...poiResponse("天坛公园", "scenic", 116.4102, 39.9201).pois[0],
                    amapId: "poi_天坛公园"
                  }
                }
              ]
            }
          ],
          routeWarnings: ["只刷新替换地点前后路线。"]
        },
        patch: { id: "patch_map_replace", validationStatus: "accepted" },
        version: { id: "ver_replace_2", versionNumber: 2, sourceType: "manual" },
        pendingPoiCandidates: [],
        validationErrors: []
      });
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<ReplacementMapHarness />);

  await waitFor(() => expect(mapApi.setCenter).toHaveBeenCalledWith([116.397026, 39.918058], true, 0));
  expect(mapApi.setFitView).not.toHaveBeenCalled();
  await waitFor(() => expect(mapApi.setZoomAndCenter).toHaveBeenCalledTimes(1));
  mapApi.setFitView.mockClear();
  mapApi.setZoomAndCenter.mockClear();

  fireEvent.change(await screen.findByPlaceholderText("搜索景点 / 餐厅 / 体验"), { target: { value: "天坛" } });
  fireEvent.click(screen.getByText("搜索"));
  await waitFor(() => expect(screen.getAllByText("天坛公园").length).toBeGreaterThan(0));
  fireEvent.click(screen.getAllByRole("button", { name: /天坛公园/ })[0]);
  fireEvent.click(screen.getByText("替换当前地点"));

  await waitFor(() => expect(screen.getAllByText("已将当前时间轴地点替换为：天坛公园").length).toBeGreaterThan(0));
  expect(plannerStore.getSnapshot().activeVersionId).toBe("ver_replace_2");
  expect(plannerStore.getSnapshot().selectedSegmentId).toBe("seg_existing");
  expect(plannerStore.getSnapshot().itineraryPlan?.days[0].segments[0].poi.name).toBe("天坛公园");
  expect(plannerStore.getSnapshot().routeWarnings).toEqual(["只刷新替换地点前后路线。"]);
  expect(mapApi.setFitView).not.toHaveBeenCalled();
  expect(mapApi.setZoomAndCenter).not.toHaveBeenCalled();

  fireEvent.click(screen.getByLabelText("选择 颐和园"));
  await waitFor(() => expect(screen.getByText("当前选中：颐和园")).toBeTruthy());
  await waitFor(() => expect(screen.getAllByText("已将当前时间轴地点替换为：天坛公园")).toHaveLength(1));
});

test("consecutive timeline POI replacements use the latest itinerary version", async () => {
  vi.stubGlobal("AMap", {
    Map: vi.fn(function Map() {
      return {
        destroy: vi.fn(),
        getZoom: vi.fn(() => 13),
        lngLatToContainer: vi.fn(() => ({ x: 320, y: 240 })),
        on: vi.fn(),
        panTo: vi.fn(),
        setCenter: vi.fn(),
        setFitView: vi.fn(),
        setZoomAndCenter: vi.fn()
      };
    }),
    Marker: vi.fn(function Marker() {
      return { setMap: vi.fn(), on: vi.fn() };
    }),
    Polyline: vi.fn(function Polyline() {
      return {};
    }),
    Circle: vi.fn(function Circle() {
      return { setMap: vi.fn() };
    })
  });

  const firstPoi = poiResponse("天坛公园", "scenic", 116.4102, 39.9201).pois[0];
  const secondPoi = poiResponse("北海公园", "scenic", 116.3881, 39.925).pois[0];
  const existingPlan = {
    ...itineraryPlanFixture(),
    days: [
      {
        ...itineraryPlanFixture().days[0],
        segments: [
          ...itineraryPlanFixture().days[0].segments,
          {
            id: "seg_second",
            startTime: "10:00",
            endTime: "11:00",
            kind: "activity",
            durationMinutes: 60,
            transportMode: "walk",
            estimatedCost: 0,
            notes: "",
            poi: poiResponse("颐和园", "scenic", 116.2755, 39.9999).pois[0]
          }
        ]
      }
    ]
  };
  const firstResultPlan = {
    ...existingPlan,
    days: [
      {
        ...existingPlan.days[0],
        segments: [
          {
            ...existingPlan.days[0].segments[0],
            poi: { ...firstPoi, amapId: "poi_天坛公园" }
          },
          existingPlan.days[0].segments[1]
        ]
      }
    ]
  };
  const secondResultPlan = {
    ...firstResultPlan,
    days: [
      {
        ...firstResultPlan.days[0],
        segments: [
          firstResultPlan.days[0].segments[0],
          {
            ...firstResultPlan.days[0].segments[1],
            poi: { ...secondPoi, amapId: "poi_北海公园" }
          }
        ]
      }
    ]
  };

  plannerStore.setState({
    itineraryPlan: existingPlan,
    activeVersionId: "ver_replace_1",
    selectedDayNumber: 1,
    selectedSegmentId: "seg_existing"
  });

  let searchCount = 0;
  let patchCount = 0;
  const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    if (path.endsWith("/map/config")) {
      return jsonResponse({ provider: "amap", enabled: true, jsApiKey: "web-key" });
    }
    if (path.includes("/map/pois")) {
      searchCount += 1;
      return jsonResponse({ pois: [searchCount === 1 ? firstPoi : secondPoi] });
    }
    if (path.endsWith("/api/agent/sessions/current")) {
      return jsonResponse({
        sessionId: "sess_replace_retry",
        status: "active",
        city: "北京",
        title: "北京会话",
        activePlanId: firstResultPlan.id,
        activeVersionId: "ver_replace_latest",
        turns: [],
        itinerary: firstResultPlan,
        pendingPoiCandidates: []
      });
    }
    if (path.endsWith("/itineraries/plan_map_add/patch") && init?.method === "POST") {
      patchCount += 1;
      const body = JSON.parse(String(init.body ?? "{}"));
      if (patchCount === 1) {
        expect(body.baseVersionId).toBe("ver_replace_1");
        expect(body.operations[0]).toMatchObject({ op: "replace_segment_poi", segmentId: "seg_existing" });
        return jsonResponse({
          itinerary: firstResultPlan,
          patch: { id: "patch_map_replace_1", validationStatus: "accepted" },
          version: { id: "ver_replace_2", versionNumber: 2, sourceType: "manual" },
          pendingPoiCandidates: [],
          validationErrors: []
        });
      }
      if (patchCount === 2) {
        expect(body.baseVersionId).toBe("ver_replace_2");
        expect(body.operations[0]).toMatchObject({ op: "replace_segment_poi", segmentId: "seg_second" });
        return jsonResponse({ detail: "Base itinerary version is stale" }, 409);
      }
      expect(body.baseVersionId).toBe("ver_replace_latest");
      expect(body.operations[0]).toMatchObject({ op: "replace_segment_poi", segmentId: "seg_second" });
      return jsonResponse({
        itinerary: secondResultPlan,
        patch: { id: "patch_map_replace_2", validationStatus: "accepted" },
        version: { id: "ver_replace_3", versionNumber: 3, sourceType: "manual" },
        pendingPoiCandidates: [],
        validationErrors: []
      });
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  function StoreBackedMapHarness() {
    const [snapshot, setSnapshot] = useState(plannerStore.getSnapshot());
    useEffect(() => plannerStore.subscribe(setSnapshot), []);
    return (
      <PlannerMap
        plan={snapshot.itineraryPlan}
        selectedSegmentId={snapshot.selectedSegmentId}
        onSelectSegment={(segmentId) => plannerStore.setState({ selectedSegmentId: segmentId })}
        city="北京"
      />
    );
  }

  render(<StoreBackedMapHarness />);

  fireEvent.change(await screen.findByPlaceholderText("搜索景点 / 餐厅 / 体验"), { target: { value: "天坛" } });
  fireEvent.click(screen.getByText("搜索"));
  await waitFor(() => expect(screen.getAllByText("天坛公园").length).toBeGreaterThan(0));
  fireEvent.click(screen.getAllByRole("button", { name: /天坛公园/ })[0]);
  fireEvent.click(screen.getByText("替换当前地点"));
  await waitFor(() => expect(plannerStore.getSnapshot().activeVersionId).toBe("ver_replace_2"));

  plannerStore.setState({ selectedSegmentId: "seg_second", selectedMapPoi: null });
  fireEvent.change(screen.getByPlaceholderText("搜索景点 / 餐厅 / 体验"), { target: { value: "北海" } });
  fireEvent.click(screen.getByText("搜索"));
  await waitFor(() => expect(screen.getAllByText("北海公园").length).toBeGreaterThan(0));
  fireEvent.click(screen.getAllByRole("button", { name: /北海公园/ })[0]);
  fireEvent.click(screen.getByText("替换当前地点"));

  await waitFor(() => expect(plannerStore.getSnapshot().activeVersionId).toBe("ver_replace_3"));
  expect(patchCount).toBe(3);
  expect(fetchMock.mock.calls.some((call) => String(call[0]).endsWith("/api/agent/sessions/current"))).toBe(true);
  expect(plannerStore.getSnapshot().itineraryPlan?.days[0].segments[1].poi.name).toBe("北海公园");
});

test("map POI replacement conflict refreshes selection when target segment no longer exists", async () => {
  vi.stubGlobal("AMap", {
    Map: vi.fn(function Map() {
      return {
        destroy: vi.fn(),
        getZoom: vi.fn(() => 13),
        lngLatToContainer: vi.fn(() => ({ x: 320, y: 240 })),
        on: vi.fn(),
        panTo: vi.fn(),
        setCenter: vi.fn(),
        setFitView: vi.fn(),
        setZoomAndCenter: vi.fn()
      };
    }),
    Marker: vi.fn(function Marker() {
      return { setMap: vi.fn(), on: vi.fn() };
    }),
    Polyline: vi.fn(function Polyline() {
      return {};
    }),
    Circle: vi.fn(function Circle() {
      return { setMap: vi.fn() };
    })
  });
  const existingPlan = itineraryPlanFixture();
  const latestPlan = {
    ...existingPlan,
    days: [
      {
        ...existingPlan.days[0],
        segments: [
          {
            ...existingPlan.days[0].segments[0],
            id: "seg_latest",
            poi: poiResponse("景山公园", "park", 116.3969, 39.9236).pois[0]
          }
        ]
      }
    ]
  };
  plannerStore.setState({
    itineraryPlan: existingPlan,
    activeVersionId: "ver_replace_1",
    selectedDayNumber: 1,
    selectedSegmentId: "seg_existing"
  });
  let patchCount = 0;
  const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    if (path.endsWith("/map/config")) {
      return jsonResponse({ provider: "amap", enabled: true, jsApiKey: "web-key" });
    }
    if (path.includes("/map/pois")) {
      return jsonResponse(poiResponse("天坛公园", "scenic", 116.4102, 39.9201));
    }
    if (path.endsWith("/api/agent/sessions/current")) {
      return jsonResponse({
        sessionId: "sess_replace_deleted",
        status: "active",
        city: "北京",
        title: "北京会话",
        activePlanId: latestPlan.id,
        activeVersionId: "ver_latest",
        turns: [],
        itinerary: latestPlan,
        pendingPoiCandidates: []
      });
    }
    if (path.endsWith("/itineraries/plan_map_add/patch") && init?.method === "POST") {
      patchCount += 1;
      return jsonResponse({ detail: "Base itinerary version is stale" }, 409);
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  function StoreBackedMapHarness() {
    const [snapshot, setSnapshot] = useState(plannerStore.getSnapshot());
    useEffect(() => plannerStore.subscribe(setSnapshot), []);
    return (
      <PlannerMap
        plan={snapshot.itineraryPlan}
        selectedSegmentId={snapshot.selectedSegmentId}
        onSelectSegment={(segmentId) => plannerStore.setState({ selectedSegmentId: segmentId })}
        city="北京"
      />
    );
  }

  render(<StoreBackedMapHarness />);

  fireEvent.change(await screen.findByPlaceholderText("搜索景点 / 餐厅 / 体验"), { target: { value: "天坛" } });
  fireEvent.click(screen.getByText("搜索"));
  await waitFor(() => expect(screen.getAllByText("天坛公园").length).toBeGreaterThan(0));
  fireEvent.click(screen.getAllByRole("button", { name: /天坛公园/ })[0]);
  fireEvent.click(screen.getByText("替换当前地点"));

  await waitFor(() =>
    expect(screen.getAllByText("行程已更新，请基于最新时间轴重新选择要替换的地点。").length).toBeGreaterThan(0)
  );
  expect(patchCount).toBe(1);
  expect(plannerStore.getSnapshot().activeVersionId).toBe("ver_latest");
  expect(plannerStore.getSnapshot().selectedSegmentId).toBe("seg_latest");
  expect(plannerStore.getSnapshot().selectedRouteOptionId).toBeNull();
  expect(plannerStore.getSnapshot().previewRouteOptionId).toBeNull();
});

test("stale map add response does not overwrite a newer active itinerary version", async () => {
  vi.stubGlobal("AMap", {
    Map: vi.fn(function Map() {
      return {
        destroy: vi.fn(),
        getZoom: vi.fn(() => 12),
        lngLatToContainer: vi.fn(() => ({ x: 320, y: 240 })),
        on: vi.fn(),
        setFitView: vi.fn(),
        setZoomAndCenter: vi.fn()
      };
    }),
    Marker: vi.fn(function Marker() {
      return { setMap: vi.fn(), on: vi.fn() };
    }),
    Polyline: vi.fn(function Polyline() {
      return {};
    })
  });

  const existingPlan = itineraryPlanFixture();
  let resolvePatch: ((response: Response) => void) | undefined;
  plannerStore.setState({
    itineraryPlan: existingPlan,
    activeVersionId: "ver_map_1",
    selectedDayNumber: 1,
    selectedSegmentId: "seg_existing"
  });

  const fetchMock = vi.fn((url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    if (path.endsWith("/map/config")) {
      return Promise.resolve(jsonResponse({ provider: "amap", enabled: true, jsApiKey: "web-key" }));
    }
    if (path.includes("/map/pois")) {
      return Promise.resolve(jsonResponse(poiResponse("颐和园", "scenic", 116.2755, 39.9999)));
    }
    if (path.endsWith("/itineraries/plan_map_add/patch") && init?.method === "POST") {
      return new Promise<Response>((resolve) => {
        resolvePatch = resolve;
      });
    }
    return Promise.resolve(jsonResponse({}, 404));
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<PlannerMap plan={existingPlan} selectedSegmentId="seg_existing" onSelectSegment={() => undefined} city="北京" />);

  fireEvent.change(await screen.findByPlaceholderText("搜索景点 / 餐厅 / 体验"), { target: { value: "颐和园" } });
  fireEvent.click(screen.getByText("搜索"));
  await waitFor(() => expect(screen.getAllByText("颐和园").length).toBeGreaterThan(0));
  fireEvent.click(screen.getAllByRole("button", { name: /颐和园/ })[0]);
  fireEvent.click(screen.getByText("加入当前 Day"));
  await waitFor(() => expect(resolvePatch).toBeTruthy());

  plannerStore.setState({ activeVersionId: "ver_newer" });
  await act(async () => {
    resolvePatch?.(
      jsonResponse({
        itinerary: {
          ...existingPlan,
          days: [
            {
              ...existingPlan.days[0],
              segments: [
                ...existingPlan.days[0].segments,
                {
                  id: "seg_stale_map",
                  dayId: "day_map_1",
                  startTime: "10:00",
                  endTime: "10:30",
                  durationMinutes: 30,
                  transportMode: "walk",
                  estimatedCost: 0,
                  walkingDistanceMeters: 0,
                  notes: "旧响应",
                  poi: {
                    ...poiResponse("颐和园", "scenic", 116.2755, 39.9999).pois[0],
                    amapId: "poi_颐和园"
                  }
                }
              ]
            }
          ]
        },
        patch: { id: "patch_stale_map", validationStatus: "accepted" },
        version: { id: "ver_stale", versionNumber: 2, sourceType: "manual" },
        pendingPoiCandidates: [],
        validationErrors: []
      })
    );
  });

  const snapshot = plannerStore.getSnapshot();
  expect(snapshot.activeVersionId).toBe("ver_newer");
  expect(snapshot.selectedSegmentId).toBe("seg_existing");
  expect(snapshot.itineraryPlan?.days[0].segments.some((segment) => segment.id === "seg_stale_map")).toBe(false);
  expect(screen.queryByText("已加入 Day 1：颐和园")).toBeNull();
});

test("base map reverse lookup rejects far away POIs instead of showing the wrong place", async () => {
  const mapClickHandlers: Array<(event?: Record<string, unknown>) => void> = [];
  vi.stubGlobal("AMap", {
    Map: vi.fn(function Map() {
      return {
        destroy: vi.fn(),
        getZoom: vi.fn(() => 12),
        lngLatToContainer: vi.fn(() => ({ x: 320, y: 240 })),
        on: vi.fn((eventName: string, handler: (event?: Record<string, unknown>) => void) => {
          if (eventName === "click") {
            mapClickHandlers.push(handler);
          }
        }),
        setZoomAndCenter: vi.fn()
      };
    }),
    Marker: vi.fn(function Marker() {
      return { setMap: vi.fn(), on: vi.fn() };
    }),
    Polyline: vi.fn(function Polyline() {
      return {};
    })
  });
  vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/map/config")) {
      return jsonResponse({ provider: "amap", enabled: true, jsApiKey: "web-key" });
    }
    if (path.includes("/map/pois/nearby")) {
      return jsonResponse(poiResponse("偏移很远的地点", "scenic", 116.50, 39.99));
    }
    return jsonResponse({}, 404);
  }));

  render(<PlannerMap plan={null} selectedSegmentId={null} onSelectSegment={() => undefined} city="北京" />);
  await waitFor(() => expect(mapClickHandlers.length).toBeGreaterThan(0));

  mapClickHandlers[0]?.({
    lnglat: {
      getLng: () => 116.41,
      getLat: () => 39.92
    }
  });

  await waitFor(() => expect(screen.getByText("未能准确识别该地点，请点击更明确的地名或使用搜索。")).toBeTruthy());
  expect(screen.queryByText("偏移很远的地点")).toBeNull();
});

test("base map reverse lookup rejects nearby POIs when the clicked label does not match", async () => {
  const mapClickHandlers: Array<(event?: Record<string, unknown>) => void> = [];
  vi.stubGlobal("AMap", {
    Map: vi.fn(function Map() {
      return {
        destroy: vi.fn(),
        getZoom: vi.fn(() => 12),
        lngLatToContainer: vi.fn(() => ({ x: 320, y: 240 })),
        on: vi.fn((eventName: string, handler: (event?: Record<string, unknown>) => void) => {
          if (eventName === "click") {
            mapClickHandlers.push(handler);
          }
        }),
        setZoomAndCenter: vi.fn()
      };
    }),
    Marker: vi.fn(function Marker() {
      return { setMap: vi.fn(), on: vi.fn() };
    }),
    Polyline: vi.fn(function Polyline() {
      return {};
    })
  });
  vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/map/config")) {
      return jsonResponse({ provider: "amap", enabled: true, jsApiKey: "web-key" });
    }
    if (path.includes("/map/pois/nearby")) {
      return jsonResponse(poiResponse("清华大学", "education", 116.32672, 40.00342));
    }
    return jsonResponse({}, 404);
  }));

  render(<PlannerMap plan={null} selectedSegmentId={null} onSelectSegment={() => undefined} city="北京" />);
  await waitFor(() => expect(mapClickHandlers.length).toBeGreaterThan(0));

  mapClickHandlers[0]?.({
    poi: {
      name: "北京大学",
      location: {
        getLng: () => 116.32672,
        getLat: () => 40.00342
      }
    }
  });

  await waitFor(() => expect(screen.getByText("未能准确识别该地点，请点击更明确的地名或使用搜索。")).toBeTruthy());
  expect(screen.queryByText("清华大学")).toBeNull();
  expect(plannerStore.getSnapshot().selectedMapPoi).toBeNull();
});

test("route preview updates only preview polyline without fitting view or creating AMap markers", async () => {
  const mapApi = {
    destroy: vi.fn(),
    getZoom: vi.fn(() => 12),
    lngLatToContainer: vi.fn(() => ({ x: 360, y: 260 })),
    on: vi.fn(),
    panTo: vi.fn(),
    setCenter: vi.fn(),
    setFitView: vi.fn(),
    setZoomAndCenter: vi.fn(),
    setPitch: vi.fn(),
    setRotation: vi.fn(),
    zoomIn: vi.fn(),
    zoomOut: vi.fn()
  };
  const markerConstructor = vi.fn(function Marker() {
    return { setMap: vi.fn(), on: vi.fn(), setOptions: vi.fn() };
  });
  const polylineConstructor = vi.fn(function Polyline() {
    return { setMap: vi.fn(), setOptions: vi.fn() };
  });
  vi.stubGlobal("AMap", {
    Map: vi.fn(function Map() {
      return mapApi;
    }),
    Marker: markerConstructor,
    Polyline: polylineConstructor
  });
  vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/map/config")) {
      return jsonResponse({ provider: "amap", enabled: true, jsApiKey: "web-key" });
    }
    return jsonResponse({}, 404);
  }));

  const plan = itineraryPlanWithRoutesFixture();
  const { rerender } = render(
    <PlannerMap
      plan={plan}
      selectedSegmentId="seg_route_1"
      selectedRouteOptionId="route_fast"
      onSelectSegment={() => undefined}
      city="北京"
    />
  );
  expect(markerConstructor).not.toHaveBeenCalled();
  const polylineCountAfterInitialDraw = polylineConstructor.mock.calls.length;
  mapApi.setFitView.mockClear();

  rerender(
    <PlannerMap
      plan={plan}
      selectedSegmentId="seg_route_1"
      selectedRouteOptionId="route_fast"
      previewRouteOptionId="route_preview"
      onSelectSegment={() => undefined}
      city="北京"
    />
  );

  await waitFor(() => expect(polylineConstructor.mock.calls.length).toBeGreaterThan(polylineCountAfterInitialDraw));
  expect(markerConstructor).not.toHaveBeenCalled();
  expect(mapApi.setFitView).not.toHaveBeenCalled();
});

test("itinerary map keeps low-confidence Agent POI marker labels name-only", async () => {
  const mapApi = {
    destroy: vi.fn(),
    getZoom: vi.fn(() => 12),
    lngLatToContainer: vi.fn(() => ({ x: 360, y: 260 })),
    on: vi.fn(),
    panTo: vi.fn(),
    setCenter: vi.fn(),
    setFitView: vi.fn(),
    setPitch: vi.fn(),
    setRotation: vi.fn(),
    zoomIn: vi.fn(),
    zoomOut: vi.fn()
  };
  const markerConstructor = vi.fn(function Marker() {
    return { setMap: vi.fn(), on: vi.fn(), setOptions: vi.fn() };
  });
  vi.stubGlobal("AMap", {
    Map: vi.fn(function Map() {
      return mapApi;
    }),
    Marker: markerConstructor,
    Polyline: vi.fn(function Polyline() {
      return { setMap: vi.fn(), setOptions: vi.fn() };
    })
  });
  vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/map/config")) {
      return jsonResponse({ provider: "amap", enabled: true, jsApiKey: "web-key" });
    }
    return jsonResponse({}, 404);
  }));

  const plan = itineraryPlanWithRoutesFixture();
  plan.days[0].segments[1].poi = {
    ...plan.days[0].segments[1].poi,
    source: "agent-text-timeline",
    confidence: 0.4,
    sourceNote: "高德PO待校验 置信度40% - Agent候选，待高德 POI grounding;开放预约待确认 <script>alert(1)</script>"
  };

  render(<PlannerMap plan={plan} selectedSegmentId="seg_route_1" onSelectSegment={() => undefined} city="北京" />);

  await waitFor(() => expect(screen.getByText("当前选中：故宫博物院")).toBeTruthy());
  expect(markerConstructor).not.toHaveBeenCalled();
  expect(screen.queryByText("pending-grounding")).toBeNull();
  expect(screen.queryByText("高德PO待校验")).toBeNull();
  expect(screen.queryByText("Agent候选")).toBeNull();
  expect(screen.queryByText("<script>alert(1)</script>")).toBeNull();
});

test("selected itinerary marker uses DOM markers without AMap marker mutation", async () => {
  const mapApi = {
    add: vi.fn(() => {
      throw new Error("Cannot read properties of undefined (reading 'add')");
    }),
    destroy: vi.fn(),
    getZoom: vi.fn(() => 12),
    lngLatToContainer: vi.fn(() => ({ x: 360, y: 260 })),
    on: vi.fn(),
    panTo: vi.fn(),
    setCenter: vi.fn(),
    setFitView: vi.fn(),
    setPitch: vi.fn(),
    setRotation: vi.fn(),
    zoomIn: vi.fn(),
    zoomOut: vi.fn()
  };
  const markerConstructor = vi.fn(function Marker() {
    throw new Error("Cannot read properties of undefined (reading 'getOptions')");
  });
  const mapConstructor = vi.fn(function Map() {
    return mapApi;
  });
  vi.stubGlobal("AMap", {
    Map: mapConstructor,
    Marker: markerConstructor,
    Polyline: vi.fn(function Polyline() {
      return { setMap: vi.fn(), setOptions: vi.fn() };
    })
  });
  vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/map/config")) {
      return jsonResponse({ provider: "amap", enabled: true, jsApiKey: "web-key" });
    }
    return jsonResponse({}, 404);
  }));

  const plan = itineraryPlanWithRoutesFixture();
  const { rerender } = render(<PlannerMap plan={plan} selectedSegmentId="seg_route_1" onSelectSegment={() => undefined} city="北京" />);

  await waitFor(() => expect(screen.getByLabelText("选择 故宫博物院").className).toContain("selected"));
  expect(mapConstructor).toHaveBeenCalledTimes(1);
  expect(markerConstructor).not.toHaveBeenCalled();

  rerender(<PlannerMap plan={plan} selectedSegmentId="seg_route_2" onSelectSegment={() => undefined} city="北京" />);

  await waitFor(() => expect(screen.getByLabelText("选择 天坛公园").className).toContain("selected"));
  expect(screen.getByLabelText("选择 故宫博物院").className).not.toContain("selected");
  expect(mapConstructor).toHaveBeenCalledTimes(1);
  expect(mapApi.destroy).not.toHaveBeenCalled();
  expect(markerConstructor).not.toHaveBeenCalled();
  expect(mapApi.add).not.toHaveBeenCalled();
});

test("density map compares only the scoped slot candidates with ordered day anchors", async () => {
  const mapClickHandlers: Array<(event?: unknown) => void> = [];
  const mapApi = {
    destroy: vi.fn(),
    getZoom: vi.fn(() => 12),
    lngLatToContainer: vi.fn(() => ({ x: 360, y: 260 })),
    on: vi.fn((eventName: string, handler: (event?: unknown) => void) => {
      if (eventName === "click") {
        mapClickHandlers.push(handler);
      }
    }),
    panTo: vi.fn(),
    setCenter: vi.fn(),
    setFitView: vi.fn(),
    setZoomAndCenter: vi.fn(),
    setPitch: vi.fn(),
    setRotation: vi.fn(),
    zoomIn: vi.fn(),
    zoomOut: vi.fn()
  };
  vi.stubGlobal("AMap", {
    Map: vi.fn(function Map() {
      return mapApi;
    }),
    Marker: vi.fn(function Marker() {
      throw new Error("AMap Marker should not be constructed for density comparison pins");
    }),
    Polyline: vi.fn(function Polyline() {
      return { setMap: vi.fn(), setOptions: vi.fn() };
    })
  });
  const fetchMock = vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/map/config")) {
      return jsonResponse({ provider: "amap", enabled: true, jsApiKey: "web-key" });
    }
    if (path.includes("/map/pois/nearby")) {
      return jsonResponse(poiResponse("烟袋斜街", "scenic", 116.388, 39.94));
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  const shichahai = {
    ...poiResponse("什刹海", "scenic", 116.385, 39.941).pois[0],
    amapId: "B000A7O5PK"
  };
  const nanluoguxiang = {
    ...poiResponse("南锣鼓巷", "scenic", 116.403, 39.936).pois[0],
    amapId: "B000A8UIN8"
  };
  const dayTwoCandidate = {
    ...poiResponse("前门大街", "scenic", 116.397, 39.899).pois[0],
    amapId: "B000A7B0M7"
  };
  const campus = {
    ...poiResponse("北京大学", "scenic", 116.31, 39.99).pois[0],
    amapId: "B0CAMPUS",
    startTime: "09:00"
  };
  const museum = {
    ...poiResponse("中国美术馆", "scenic", 116.41, 39.923).pois[0],
    amapId: "B0MUSEUM",
    startTime: "11:30"
  };
  plannerStore.setState({
    agentSession: {
      sessionId: "sess_density_map",
      status: "active",
      city: "北京",
      title: "北京 AI 行程",
      activePlanId: "plan_density_pending",
      activeVersionId: null,
      turns: [],
      itinerary: null,
      pendingPoiCandidates: []
    },
    pendingPoiCandidates: [
      {
        id: "cand_day_1_walk",
        query: "Day 1 街区漫步",
        city: "北京",
        category: "area_walk",
        status: "pending",
        createdAt: "2026-07-21T08:00:00Z",
        candidates: [shichahai, nanluoguxiang]
      },
      {
        id: "cand_day_2_walk",
        query: "Day 2 街区漫步",
        city: "北京",
        category: "area_walk",
        status: "pending",
        createdAt: "2026-07-21T08:00:00Z",
        candidates: [dayTwoCandidate]
      }
    ],
    selectedDayNumber: 1,
    activeDensityMapComparison: {
      sessionId: "sess_density_map",
      sourceAssistantTurnId: "turn_density",
      candidateRecordId: "cand_day_1_walk",
      briefId: "brief_day_1",
      poolId: "pool_day_1_walk",
      dayNumber: 1,
      planningSlotId: "slot_day_1_walk",
      timeWindow: "14:00-16:00",
      displayNeed: "街区漫步",
      anchors: [campus, museum],
      candidateChoices: [
        {
          amapId: "B000A7O5PK",
          sourceAssistantTurnId: "turn_density",
          choiceId: "density_day_1_shichahai",
          label: "Day 1：什刹海"
        }
      ]
    }
  });
  const confirmDensityCandidate = vi.fn();

  render(
    <PlannerMap
      plan={itineraryPlanWithRoutesFixture()}
      selectedSegmentId={null}
      onSelectSegment={() => undefined}
      onConfirmDensityCandidate={confirmDensityCandidate}
      city="北京"
    />
  );

  await screen.findByLabelText("Day 1 已安排地点 北京大学");
  expect(screen.getByLabelText("候选位置对比模式")).toBeTruthy();
  expect(screen.queryByPlaceholderText("搜索景点 / 餐厅 / 体验")).toBeNull();
  expect(screen.getByLabelText("Day 1 已安排地点 中国美术馆")).toBeTruthy();
  expect(screen.getByLabelText("选择 什刹海")).toBeTruthy();
  expect(screen.getByLabelText("选择 南锣鼓巷")).toBeTruthy();
  expect(screen.queryByLabelText("选择 前门大街")).toBeNull();
  expect(screen.queryByLabelText("选择 故宫博物院")).toBeNull();
  expect(screen.getAllByLabelText("Day 1 已安排地点 北京大学")).toHaveLength(1);
  await waitFor(() => expect(mapApi.setZoomAndCenter).toHaveBeenCalled());
  const lastFitCall =
    mapApi.setZoomAndCenter.mock.calls[mapApi.setZoomAndCenter.mock.calls.length - 1];
  const [, fittedCenter, immediately, duration] = lastFitCall;
  expect(fittedCenter[0]).toBeCloseTo(116.36);
  expect(fittedCenter[1]).toBeCloseTo(39.9565);
  expect(immediately).toBe(true);
  expect(duration).toBe(0);
  act(() => {
    mapClickHandlers[0]?.({
      lnglat: { getLng: () => 116.4, getLat: () => 39.9 }
    });
  });
  expect(
    fetchMock.mock.calls.some((call) =>
      String(call[0]).includes("/map/pois/nearby")
    )
  ).toBe(false);

  fireEvent.click(screen.getByLabelText("选择 什刹海"));
  expect(screen.getByText("仅供路线位置比较；确认仍通过该候选已有的对话选择执行。")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "确认并补入时间轴" }));
  expect(confirmDensityCandidate).toHaveBeenCalledWith(expect.objectContaining({
    amapId: "B000A7O5PK",
    sourceAssistantTurnId: "turn_density",
    choiceId: "density_day_1_shichahai"
  }));
  expect(fetchMock.mock.calls.some((call) => String(call[0]).includes("/messages"))).toBe(false);

  fireEvent.click(screen.getByLabelText("Day 1 已安排地点 北京大学"));
  expect(screen.getByLabelText("Day 1 已安排地点 北京大学").className).toContain("selected");
  fireEvent.change(screen.getByLabelText("附近搜索"), { target: { value: "胡同" } });
  fireEvent.submit(screen.getByLabelText("附近搜索").closest("form")!);
  await screen.findByLabelText("选择 烟袋斜街");
  expect(screen.getByLabelText("选择 烟袋斜街").className).not.toContain("selected");
  expect(screen.getByLabelText("Day 1 已安排地点 北京大学").className).toContain("selected");
  expect(fetchMock.mock.calls.some((call) => String(call[0]).includes("/messages"))).toBe(false);

  act(() => {
    plannerStore.setState({
      activeDensityMapComparison: {
        ...plannerStore.getSnapshot().activeDensityMapComparison!,
        candidateRecordId: "cand_missing",
        planningSlotId: "slot_missing"
      }
    });
  });
  await waitFor(() => expect(screen.queryByLabelText("选择 什刹海")).toBeNull());
  expect(screen.queryByLabelText("选择 前门大街")).toBeNull();

  act(() => {
    plannerStore.setState({
      activeDensityMapComparison: null,
      candidateMapPois: [],
      selectedMapPoi: null
    });
  });
  await waitFor(() => expect(screen.queryByLabelText("选择 什刹海")).toBeNull());
  expect(screen.queryByText("加入 Day 1")).toBeNull();
});

test("DOM markers stay projected during map movement and zoom without rebuilding the map", async () => {
  const viewportHandlers: Record<"mapmove" | "zoomchange", Array<() => void>> = {
    mapmove: [],
    zoomchange: []
  };
  let projectedPosition = { x: 300, y: 210 };
  const mapApi = {
    destroy: vi.fn(),
    getZoom: vi.fn(() => 12),
    lngLatToContainer: vi.fn(() => projectedPosition),
    on: vi.fn((eventName: string, handler: () => void) => {
      if (eventName === "mapmove" || eventName === "zoomchange") {
        viewportHandlers[eventName].push(handler);
      }
    }),
    panTo: vi.fn(),
    setCenter: vi.fn(),
    setFitView: vi.fn(),
    setPitch: vi.fn(),
    setRotation: vi.fn(),
    setZoomAndCenter: vi.fn(),
    zoomIn: vi.fn(),
    zoomOut: vi.fn()
  };
  const mapConstructor = vi.fn(function Map() {
    return mapApi;
  });
  vi.stubGlobal("AMap", {
    Map: mapConstructor,
    Marker: vi.fn(function Marker() {
      throw new Error("AMap Marker should not be constructed for map POI pins");
    }),
    Polyline: vi.fn(function Polyline() {
      return { setMap: vi.fn(), setOptions: vi.fn() };
    })
  });
  vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/map/config")) {
      return jsonResponse({ provider: "amap", enabled: true, jsApiKey: "web-key" });
    }
    return jsonResponse({}, 404);
  }));

  render(<PlannerMap plan={itineraryPlanWithRoutesFixture()} selectedSegmentId="seg_route_1" onSelectSegment={() => undefined} city="北京" />);

  await screen.findByLabelText("选择 故宫博物院");
  await waitFor(() => expect(screen.getByLabelText("选择 故宫博物院").style.transform).toContain("translate3d(300px, 210px, 0)"));
  expect(screen.getByLabelText("选择 故宫博物院").style.visibility).toBe("visible");
  expect(mapConstructor).toHaveBeenCalledTimes(1);
  expect(viewportHandlers.mapmove).toHaveLength(1);
  expect(viewportHandlers.zoomchange).toHaveLength(1);

  projectedPosition = { x: 460, y: 260 };
  act(() => {
    viewportHandlers.mapmove[0]?.();
  });

  await waitFor(() => expect(screen.getByLabelText("选择 故宫博物院").style.transform).toContain("translate3d(460px, 260px, 0)"));
  expect(screen.getByLabelText("选择 故宫博物院").style.visibility).toBe("visible");

  projectedPosition = { x: 500, y: 300 };
  act(() => {
    viewportHandlers.zoomchange[0]?.();
  });

  await waitFor(() => expect(screen.getByLabelText("选择 故宫博物院").style.transform).toContain("translate3d(500px, 300px, 0)"));
  expect(screen.getByLabelText("选择 故宫博物院").style.visibility).toBe("visible");
  expect(mapConstructor).toHaveBeenCalledTimes(1);
  expect(mapApi.destroy).not.toHaveBeenCalled();
});

test("map drag closes overlays locally and delays store sync until movement settles", async () => {
  const dragHandlers: Array<() => void> = [];
  const moveEndHandlers: Array<() => void> = [];
  const mapApi = {
    destroy: vi.fn(),
    getZoom: vi.fn(() => 12),
    lngLatToContainer: vi.fn(() => ({ x: 320, y: 240 })),
    on: vi.fn((eventName: string, handler: () => void) => {
      if (eventName === "dragstart") {
        dragHandlers.push(handler);
      }
      if (eventName === "moveend") {
        moveEndHandlers.push(handler);
      }
    }),
    panTo: vi.fn(),
    setCenter: vi.fn(),
    setFitView: vi.fn(),
    setPitch: vi.fn(),
    setRotation: vi.fn(),
    setZoomAndCenter: vi.fn(),
    zoomIn: vi.fn(),
    zoomOut: vi.fn()
  };
  vi.stubGlobal("AMap", {
    Map: vi.fn(function Map() {
      return mapApi;
    }),
    Polyline: vi.fn(function Polyline() {
      return { setMap: vi.fn(), setOptions: vi.fn() };
    })
  });
  vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/map/config")) {
      return jsonResponse({ provider: "amap", enabled: true, jsApiKey: "web-key" });
    }
    return jsonResponse({}, 404);
  }));

  const plan = itineraryPlanWithRoutesFixture();
  const { container } = render(<PlannerMap plan={plan} selectedSegmentId="seg_route_1" onSelectSegment={() => undefined} city="北京" />);
  await screen.findByLabelText("选择 故宫博物院");
  await waitFor(() => expect(plannerStore.getSnapshot().candidateMapPois).toEqual([]));
  plannerStore.setState({ selectedMapPoi: plan.days[0].segments[0].poi });
  const setStateSpy = vi.spyOn(plannerStore, "setState");
  setStateSpy.mockClear();

  act(() => {
    dragHandlers[0]?.();
  });

  expect(container.querySelector(".map-stage")?.className).toContain("map-interacting");
  expect(setStateSpy).not.toHaveBeenCalledWith(expect.objectContaining({ selectedMapPoi: null }));

  act(() => {
    moveEndHandlers[0]?.();
  });

  await waitFor(() => expect(setStateSpy).toHaveBeenCalledWith(expect.objectContaining({ selectedMapPoi: null })));
  await waitFor(() => expect(container.querySelector(".map-stage")?.className).not.toContain("map-interacting"));
});

test("transit routes render step polylines separately instead of one merged line", async () => {
  const mapApi = {
    destroy: vi.fn(),
    getZoom: vi.fn(() => 12),
    lngLatToContainer: vi.fn(() => ({ x: 360, y: 260 })),
    on: vi.fn(),
    panTo: vi.fn(),
    setCenter: vi.fn(),
    setFitView: vi.fn(),
    setPitch: vi.fn(),
    setRotation: vi.fn(),
    zoomIn: vi.fn(),
    zoomOut: vi.fn()
  };
  const polylineOptions: Array<Record<string, unknown>> = [];
  vi.stubGlobal("AMap", {
    Map: vi.fn(function Map() {
      return mapApi;
    }),
    Marker: vi.fn(function Marker() {
      return { setMap: vi.fn(), on: vi.fn(), setOptions: vi.fn() };
    }),
    Polyline: vi.fn(function Polyline(options: Record<string, unknown>) {
      polylineOptions.push(options);
      return { setMap: vi.fn(), setOptions: vi.fn() };
    })
  });
  vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/map/config")) {
      return jsonResponse({ provider: "amap", enabled: true, jsApiKey: "web-key" });
    }
    return jsonResponse({}, 404);
  }));

  const plan = itineraryPlanWithRoutesFixture();
  plan.routeOptions = [{
    ...routeOptionFixture("route_transit_steps", true, 1080, 4),
    steps: [
      { mode: "walking", polyline: "116.397026,39.918058;116.398,39.919" },
      { mode: "transit", polyline: "116.398,39.919;116.406,39.9198" },
      { mode: "walking", polyline: "116.406,39.9198;116.4102,39.9201" }
    ]
  }];

  render(<PlannerMap plan={plan} selectedSegmentId="seg_route_1" onSelectSegment={() => undefined} city="北京" />);

  await waitFor(() => expect(polylineOptions).toHaveLength(3));
  expect(polylineOptions.map((options) => options.strokeStyle)).toEqual(["dashed", "solid", "dashed"]);
  expect(polylineOptions.map((options) => options.path)).not.toContainEqual(plan.routeOptions[0].polyline);
});

test("map does not draw legacy POI-id route when current segment leg is missing", async () => {
  const mapApi = {
    destroy: vi.fn(),
    getZoom: vi.fn(() => 12),
    lngLatToContainer: vi.fn(() => ({ x: 360, y: 260 })),
    on: vi.fn(),
    panTo: vi.fn(),
    setCenter: vi.fn(),
    setFitView: vi.fn(),
    setPitch: vi.fn(),
    setRotation: vi.fn(),
    zoomIn: vi.fn(),
    zoomOut: vi.fn()
  };
  const polylineOptions: Array<Record<string, unknown>> = [];
  vi.stubGlobal("AMap", {
    Map: vi.fn(function Map() {
      return mapApi;
    }),
    Marker: vi.fn(function Marker() {
      return { setMap: vi.fn(), on: vi.fn(), setOptions: vi.fn() };
    }),
    Polyline: vi.fn(function Polyline(options: Record<string, unknown>) {
      polylineOptions.push(options);
      return { setMap: vi.fn(), setOptions: vi.fn() };
    })
  });
  vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/map/config")) {
      return jsonResponse({ provider: "amap", enabled: true, jsApiKey: "web-key" });
    }
    return jsonResponse({}, 404);
  }));

  const plan = itineraryPlanWithRoutesFixture();
  plan.routeOptions = [
    {
      ...routeOptionFixture("route_legacy_poi_only", true, 900, 32),
      fromSegmentId: "seg_legacy_from",
      toSegmentId: "seg_legacy_to"
    }
  ];

  render(<PlannerMap plan={plan} selectedSegmentId="seg_route_1" onSelectSegment={() => undefined} city="北京" />);

  await waitFor(() => expect(screen.getByText("路线待地点确认后生成")).toBeTruthy());
  expect(polylineOptions).toHaveLength(0);
});

test("selected route changes rebuild route polylines without AMap setOptions overlay scan", async () => {
  const mapApi = {
    destroy: vi.fn(),
    getZoom: vi.fn(() => 12),
    lngLatToContainer: vi.fn(() => ({ x: 360, y: 260 })),
    on: vi.fn(),
    panTo: vi.fn(),
    setCenter: vi.fn(),
    setFitView: vi.fn(),
    setPitch: vi.fn(),
    setRotation: vi.fn(),
    zoomIn: vi.fn(),
    zoomOut: vi.fn()
  };
  const polylineOverlays: Array<{ setMap: ReturnType<typeof vi.fn>; setOptions: ReturnType<typeof vi.fn> }> = [];
  const polylineOptions: Array<Record<string, unknown>> = [];
  const markerConstructor = vi.fn(function Marker() {
    throw new Error("Cannot read properties of undefined (reading 'getOptions')");
  });
  const polylineConstructor = vi.fn(function Polyline(options: Record<string, unknown>) {
    polylineOptions.push(options);
    const overlay = {
      setMap: vi.fn(),
      setOptions: vi.fn(() => {
        throw new Error("Cannot read properties of undefined (reading 'getOptions')");
      })
    };
    polylineOverlays.push(overlay);
    return overlay;
  });
  vi.stubGlobal("AMap", {
    Map: vi.fn(function Map() {
      return mapApi;
    }),
    Marker: markerConstructor,
    Polyline: polylineConstructor
  });
  vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/map/config")) {
      return jsonResponse({ provider: "amap", enabled: true, jsApiKey: "web-key" });
    }
    return jsonResponse({}, 404);
  }));

  const plan = itineraryPlanWithRoutesFixture();
  const { rerender } = render(
    <PlannerMap plan={plan} selectedSegmentId="seg_route_1" selectedRouteOptionId={null} onSelectSegment={() => undefined} city="北京" />
  );
  await waitFor(() => expect(polylineConstructor.mock.calls.length).toBe(1));
  const polylineCountAfterInitialDraw = polylineConstructor.mock.calls.length;
  expect(markerConstructor).not.toHaveBeenCalled();
  mapApi.setFitView.mockClear();

  rerender(
    <PlannerMap
      plan={plan}
      selectedSegmentId="seg_route_1"
      selectedRouteOptionId="route_fast"
      onSelectSegment={() => undefined}
      city="北京"
    />
  );

  await waitFor(() => expect(polylineConstructor.mock.calls.length).toBeGreaterThan(polylineCountAfterInitialDraw));
  expect(markerConstructor).not.toHaveBeenCalled();
  expect(polylineOverlays.every((overlay) => overlay.setOptions.mock.calls.length === 0)).toBe(true);
  expect(polylineOptions.some((options) => options.strokeWeight === 8 && options.strokeOpacity === 0.98)).toBe(true);
  expect(polylineOverlays.slice(0, polylineCountAfterInitialDraw).every((overlay) => overlay.setMap.mock.calls.some(([map]) => map === null))).toBe(true);
  expect(mapApi.setFitView).not.toHaveBeenCalled();
});

test("timeline marker selection avoids AMap marker construction and map.add overlay scan", async () => {
  const mapApi = {
    add: vi.fn(() => {
      throw new Error("Cannot read properties of undefined (reading 'add')");
    }),
    destroy: vi.fn(),
    getZoom: vi.fn(() => 12),
    lngLatToContainer: vi.fn(() => ({ x: 360, y: 260 })),
    on: vi.fn(),
    panTo: vi.fn(),
    setCenter: vi.fn(),
    setFitView: vi.fn(),
    setPitch: vi.fn(),
    setRotation: vi.fn(),
    setZoomAndCenter: vi.fn(),
    zoomIn: vi.fn(),
    zoomOut: vi.fn()
  };
  const markerConstructor = vi.fn(function Marker() {
    throw new Error("Cannot read properties of undefined (reading 'getOptions')");
  });
  vi.stubGlobal("AMap", {
    Map: vi.fn(function Map() {
      return mapApi;
    }),
    Marker: markerConstructor,
    Polyline: vi.fn(function Polyline() {
      return { setMap: vi.fn(), setOptions: vi.fn() };
    })
  });
  vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/map/config")) {
      return jsonResponse({ provider: "amap", enabled: true, jsApiKey: "web-key" });
    }
    return jsonResponse({}, 404);
  }));

  const plan = itineraryPlanWithRoutesFixture();
  const { rerender } = render(<PlannerMap plan={plan} selectedSegmentId="seg_route_1" onSelectSegment={() => undefined} city="北京" />);
  await waitFor(() => expect(screen.getByLabelText("选择 故宫博物院").className).toContain("selected"));
  expect(markerConstructor).not.toHaveBeenCalled();

  rerender(<PlannerMap plan={plan} selectedSegmentId="seg_route_2" onSelectSegment={() => undefined} city="北京" />);

  await waitFor(() => expect(screen.getByLabelText("选择 天坛公园").className).toContain("selected"));
  expect(screen.getByLabelText("选择 故宫博物院").className).not.toContain("selected");
  expect(markerConstructor).not.toHaveBeenCalled();
  expect(mapApi.add).not.toHaveBeenCalled();
});

test("route polyline attach failure does not crash timeline POI selection", async () => {
  const consoleWarn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
  const mapApi = {
    destroy: vi.fn(),
    getZoom: vi.fn(() => 12),
    lngLatToContainer: vi.fn(() => ({ x: 360, y: 260 })),
    on: vi.fn(),
    panTo: vi.fn(),
    setCenter: vi.fn(),
    setFitView: vi.fn(),
    setPitch: vi.fn(),
    setRotation: vi.fn(),
    setZoomAndCenter: vi.fn(),
    zoomIn: vi.fn(),
    zoomOut: vi.fn()
  };
  const polylineOptions: Array<Record<string, unknown>> = [];
  vi.stubGlobal("AMap", {
    Map: vi.fn(function Map() {
      return mapApi;
    }),
    Marker: vi.fn(function Marker() {
      throw new Error("AMap Marker should not be constructed for map POI pins");
    }),
    Polyline: vi.fn(function Polyline(options: Record<string, unknown>) {
      polylineOptions.push(options);
      return {
        setMap: vi.fn(() => {
          throw new Error("Cannot read properties of undefined (reading 'add')");
        }),
        setOptions: vi.fn()
      };
    })
  });
  vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/map/config")) {
      return jsonResponse({ provider: "amap", enabled: true, jsApiKey: "web-key" });
    }
    return jsonResponse({}, 404);
  }));

  const plan = itineraryPlanWithRoutesFixture();
  const { rerender } = render(<PlannerMap plan={plan} selectedSegmentId="seg_route_1" onSelectSegment={() => undefined} city="北京" />);

  await waitFor(() => expect(screen.getByLabelText("选择 故宫博物院").className).toContain("selected"));
  expect(polylineOptions.length).toBeGreaterThan(0);
  expect(polylineOptions.every((options) => !("map" in options))).toBe(true);
  expect(consoleWarn).toHaveBeenCalledWith("AMap Polyline attach failed", expect.any(Error));

  rerender(<PlannerMap plan={plan} selectedSegmentId="seg_route_2" onSelectSegment={() => undefined} city="北京" />);
  await waitFor(() => expect(screen.getByLabelText("选择 天坛公园").className).toContain("selected"));
});

test("route polyline cleanup failure does not crash route switching", async () => {
  const consoleWarn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
  const mapApi = {
    destroy: vi.fn(),
    getZoom: vi.fn(() => 12),
    lngLatToContainer: vi.fn(() => ({ x: 360, y: 260 })),
    on: vi.fn(),
    panTo: vi.fn(),
    setCenter: vi.fn(),
    setFitView: vi.fn(),
    setPitch: vi.fn(),
    setRotation: vi.fn(),
    setZoomAndCenter: vi.fn(),
    zoomIn: vi.fn(),
    zoomOut: vi.fn()
  };
  const polylineOverlays: Array<{ setMap: ReturnType<typeof vi.fn>; setOptions: ReturnType<typeof vi.fn> }> = [];
  vi.stubGlobal("AMap", {
    Map: vi.fn(function Map() {
      return mapApi;
    }),
    Marker: vi.fn(function Marker() {
      throw new Error("AMap Marker should not be constructed for map POI pins");
    }),
    Polyline: vi.fn(function Polyline() {
      const overlay = {
        setMap: vi.fn((nextMap: unknown) => {
          if (nextMap === null) {
            throw new Error("Cannot read properties of undefined (reading 'add')");
          }
        }),
        setOptions: vi.fn()
      };
      polylineOverlays.push(overlay);
      return overlay;
    })
  });
  vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/map/config")) {
      return jsonResponse({ provider: "amap", enabled: true, jsApiKey: "web-key" });
    }
    return jsonResponse({}, 404);
  }));

  const plan = itineraryPlanWithRoutesFixture();
  const { rerender } = render(
    <PlannerMap plan={plan} selectedSegmentId="seg_route_1" selectedRouteOptionId={null} onSelectSegment={() => undefined} city="北京" />
  );

  await waitFor(() => expect(polylineOverlays.length).toBeGreaterThan(0));

  rerender(
    <PlannerMap
      plan={plan}
      selectedSegmentId="seg_route_1"
      selectedRouteOptionId="route_fast"
      onSelectSegment={() => undefined}
      city="北京"
    />
  );

  await waitFor(() => expect(screen.getByLabelText("选择 故宫博物院").className).toContain("selected"));
  expect(consoleWarn).toHaveBeenCalledWith("AMap overlay cleanup failed", expect.any(Error));
});

test("multi-point map framing does not call AMap setFitView overlay scan", async () => {
  const mapApi = {
    destroy: vi.fn(),
    getZoom: vi.fn(() => 12),
    lngLatToContainer: vi.fn(() => ({ x: 360, y: 260 })),
    on: vi.fn(),
    panTo: vi.fn(),
    setCenter: vi.fn(),
    setFitView: vi.fn(() => {
      throw new Error("Cannot read properties of undefined (reading 'getOptions')");
    }),
    setPitch: vi.fn(),
    setRotation: vi.fn(),
    setZoomAndCenter: vi.fn(),
    zoomIn: vi.fn(),
    zoomOut: vi.fn()
  };
  vi.stubGlobal("AMap", {
    Map: vi.fn(function Map() {
      return mapApi;
    }),
    Marker: vi.fn(function Marker() {
      return { setMap: vi.fn(), on: vi.fn(), setOptions: vi.fn() };
    }),
    Polyline: vi.fn(function Polyline() {
      return { setMap: vi.fn(), setOptions: vi.fn() };
    })
  });
  vi.stubGlobal("fetch", vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url);
    if (path.endsWith("/map/config")) {
      return jsonResponse({ provider: "amap", enabled: true, jsApiKey: "web-key" });
    }
    return jsonResponse({}, 404);
  }));

  render(<PlannerMap plan={itineraryPlanWithRoutesFixture()} selectedSegmentId="seg_route_1" onSelectSegment={() => undefined} city="北京" />);

  await waitFor(() => expect(mapApi.setZoomAndCenter).toHaveBeenCalled());
  expect(mapApi.setFitView).not.toHaveBeenCalled();
});

function poiResponse(name: string, category: string, longitude = 116.397026, latitude = 39.918058, distanceMeters: number | null = null) {
  return {
    city: "北京",
    keyword: name,
    category,
    providerName: "amap-place-search",
    queriedAt: new Date().toISOString(),
    pois: [
      {
        id: `poi_${name}`,
        name,
        type: category === "food" ? "餐饮服务" : "风景名胜",
        city: "北京市",
        district: "东城区",
        address: "高德返回地址".repeat(8),
        longitude,
        latitude,
        category,
        source: "amap-place-search",
        sourceNote: "高德 WebService POI 搜索，限定当前城市，extensions=all",
        distanceMeters,
        confidence: 0.86,
        photos: [
          { title: `${name} 1`, url: `https://example.com/${name}/photo-1-with-a-very-long-source-url-that-should-not-break-the-photo-viewer-layout.jpg?from=amap&token=abcdefghijklmnopqrstuvwxyz0123456789` },
          { title: `${name} 2`, url: "https://example.com/photo-2.jpg" }
        ]
      }
    ]
  };
}

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" }
  });
}

function itineraryPlanFixture() {
  return {
    id: "plan_map_add",
    title: "北京地图测试行程",
    city: "北京",
    templateType: "agent_mvp",
    budgetEstimate: 0,
    budgetDeltaExplanation: "测试预算",
    decisionRationale: "测试行程",
    status: "draft",
    days: [
      {
        id: "day_map_1",
        dayNumber: 1,
        title: "Day 1",
        weatherSummary: "",
        riskSummary: "",
        totalEstimatedCost: 0,
        segments: [
          {
            id: "seg_existing",
            startTime: "09:00",
            endTime: "09:30",
            kind: "activity",
            durationMinutes: 30,
            transportMode: "walk",
            estimatedCost: 0,
            notes: "",
            poi: poiResponse("故宫博物院", "scenic").pois[0]
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

function itineraryPlanWithRoutesFixture() {
  const firstPoi = { ...poiResponse("故宫博物院", "scenic", 116.397026, 39.918058).pois[0], amapId: "poi_故宫博物院" };
  const secondPoi = { ...poiResponse("天坛公园", "scenic", 116.4102, 39.9201).pois[0], amapId: "poi_天坛公园" };
  return {
    id: "plan_map_routes",
    title: "北京路线预览",
    city: "北京",
    templateType: "agent_mvp",
    budgetEstimate: 0,
    budgetDeltaExplanation: "测试预算",
    decisionRationale: "测试行程",
    status: "draft",
    days: [
      {
        id: "day_route_1",
        dayNumber: 1,
        title: "Day 1",
        weatherSummary: "",
        riskSummary: "",
        totalEstimatedCost: 0,
        segments: [
          {
            id: "seg_route_1",
            startTime: "09:00",
            endTime: "10:00",
            kind: "activity",
            durationMinutes: 60,
            transportMode: "taxi",
            estimatedCost: 0,
            notes: "",
            poi: firstPoi
          },
          {
            id: "seg_route_2",
            startTime: "11:00",
            endTime: "12:00",
            kind: "activity",
            durationMinutes: 60,
            transportMode: "taxi",
            estimatedCost: 0,
            notes: "",
            poi: secondPoi
          }
        ]
      }
    ],
    routeOptions: [
      routeOptionFixture("route_fast", true, 900, 32),
      routeOptionFixture("route_preview", false, 1080, 4)
    ],
    weatherSignals: [],
    trafficCrowdingSignals: [],
    ticketLookupResults: []
  };
}

function routeOptionFixture(id: string, isSelected: boolean, durationSeconds: number, costAmount: number) {
  return {
    id,
    fromSegmentId: "seg_route_1",
    toSegmentId: "seg_route_2",
    fromPoiId: "poi_故宫博物院",
    toPoiId: "poi_天坛公园",
    provider: "amap-webservice",
    mode: costAmount > 10 ? "taxi" : "transit",
    label: costAmount > 10 ? "打车" : "公交/地铁",
    isSelected,
    sortOrder: isSelected ? 1 : 2,
    transportMode: costAmount > 10 ? "taxi" : "transit",
    distanceMeters: 6800,
    durationSeconds,
    durationMinutes: Math.round(durationSeconds / 60),
    costAmount,
    costCurrency: "CNY",
    costEstimate: costAmount,
    crowdingRisk: "low",
    source: "amap-webservice",
    polyline: [[116.397026, 39.918058], [116.4102, 39.9201]],
    steps: [] as Array<Record<string, unknown>>,
    providerPayload: {},
    error: null,
    queriedAt: "2026-06-10T10:00:00Z"
  };
}

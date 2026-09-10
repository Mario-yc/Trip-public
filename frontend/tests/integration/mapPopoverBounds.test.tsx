import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { PlannerMap } from "../../src/components/map/PlannerMap";
import { plannerStore } from "../../src/state/plannerStore";
import { createComparisonPreviewState } from "../../src/state/planComparisonPreview";

const geometry = { width: 376, height: 333, popupWidth: 196, popupHeight: 316 };
let projection: { x: number; y: number } | undefined;
const observers: Array<{ targets: Set<Element>; notify: () => void }> = [];

beforeEach(() => {
  Object.assign(geometry, { width: 376, height: 333, popupWidth: 196, popupHeight: 316 });
  projection = { x: 181, y: 161 };
  observers.length = 0;
  plannerStore.setState({
    candidateMapPois: [], selectedMapPoi: null, poiSelectionStatuses: {}, pendingPoiCandidates: [],
    activeDensityMapComparison: null, agentSession: null, itineraryPlan: null, activeVersionId: null,
    selectedDayNumber: 1, selectedSegmentId: null, timelineSelectionRequestId: 0, lastPatchError: "",
    comparisonPreview: createComparisonPreviewState()
  });
  vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockImplementation(function (this: HTMLElement) {
    return this.classList.contains("amap-base") ? geometry.width : 0;
  });
  vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockImplementation(function (this: HTMLElement) {
    return this.classList.contains("amap-base") ? geometry.height : 0;
  });
  vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockImplementation(function (this: HTMLElement) {
    return this.classList.contains("poi-map-popover") ? geometry.popupWidth : 0;
  });
  vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockImplementation(function (this: HTMLElement) {
    return this.classList.contains("poi-map-popover") ? geometry.popupHeight : 0;
  });
  vi.stubGlobal("ResizeObserver", class {
    targets = new Set<Element>();
    constructor(callback: () => void) { observers.push({ targets: this.targets, notify: callback }); }
    observe(target: Element) { this.targets.add(target); }
    disconnect() { this.targets.clear(); }
  });
});

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

async function openPopover(loading = false) {
  const mapHandlers = new Map<string, () => void>();
  const map = {
    destroy: vi.fn(), on: vi.fn((name: string, handler: () => void) => mapHandlers.set(name, handler)), resize: vi.fn(), getZoom: () => 12,
    zoomIn: vi.fn(() => mapHandlers.get("zoomstart")?.()),
    zoomOut: vi.fn(() => mapHandlers.get("zoomstart")?.()),
    lngLatToContainer: () => projection, setCenter: vi.fn(), setFitView: vi.fn(), setZoomAndCenter: vi.fn()
  };
  const createMap = vi.fn(function () { return map; });
  vi.stubGlobal("AMap", { Map: createMap, Polyline: vi.fn(function () { return { setMap: vi.fn() }; }) });
  const fetch = vi.fn(async (url: RequestInfo | URL) => {
    if (String(url).endsWith("/map/config")) {
      if (loading) return new Promise<Response>(() => undefined);
      return new Response(JSON.stringify({ provider: "amap", enabled: true, jsApiKey: "web-key" }));
    }
    if (String(url).includes("/map/pois?")) {
      return new Response(JSON.stringify({ city: "北京", keyword: "北海公园", providerName: "amap-place-search", pois: [{
        id: "poi_beihai", name: "北海公园", type: "风景名胜", city: "北京市", district: "西城区", address: "文津街1号",
        longitude: 116.389, latitude: 39.925, category: "scenic", source: "amap-place-search", photos: []
      }] }));
    }
    return new Response("{}", { status: 404 });
  });
  vi.stubGlobal("fetch", fetch);
  const view = render(<PlannerMap plan={null} selectedSegmentId={null} onSelectSegment={() => undefined} city="北京" />);
  if (!loading) await waitFor(() => expect(createMap).toHaveBeenCalledTimes(1));
  fireEvent.change(screen.getByPlaceholderText("搜索景点 / 餐厅 / 体验"), { target: { value: "北海公园" } });
  fireEvent.click(screen.getByRole("button", { name: "搜索" }));
  const card = await waitFor(() => {
    const result = view.container.querySelector<HTMLButtonElement>(".poi-strip-card");
    expect(result).not.toBeNull();
    return result!;
  });
  fireEvent.click(card);
  const popup = await waitFor(() => {
    const result = view.container.querySelector<HTMLElement>(".poi-map-popover");
    expect(result).not.toBeNull();
    return result!;
  });
  return { ...view, popup, fetch, createMap, map };
}

// jsdom has no layout engine. Apply the existing CSS anchor transform to measured
// dimensions; the separate Chromium gate checks actual getBoundingClientRect().
function expectInsideMap(popup: HTMLElement) {
  expect(popup.style.left).not.toBe("");
  expect(popup.style.top).not.toBe("");
  const left = parseFloat(popup.style.left) - geometry.popupWidth / 2;
  const top = parseFloat(popup.style.top) - geometry.popupHeight - 12;
  expect(left).toBeGreaterThanOrEqual(8);
  expect(top).toBeGreaterThanOrEqual(8);
  expect(left + geometry.popupWidth).toBeLessThanOrEqual(geometry.width - 8);
  expect(top + geometry.popupHeight).toBeLessThanOrEqual(geometry.height - 8);
}

test.each([{ x: 181, y: 161 }, { x: 0, y: 0 }, { x: 376, y: 333 }, { x: -120, y: 900 }])(
  "keeps the full popup inside the map for projected position %j without moving its marker", async (position) => {
    projection = position;
    const { popup, container } = await openPopover();
    await waitFor(() => expectInsideMap(popup));
    const marker = container.querySelector<HTMLElement>('[aria-label="选择 北海公园"]');
    expect(marker?.style.transform).toContain(`translate3d(${position.x}px, ${position.y}px, 0)`);
  }
);

test("keeps a searchable POI popup usable while the map SDK is still loading", async () => {
  const { popup, createMap } = await openPopover(true);
  expect(createMap).not.toHaveBeenCalled();
  expectInsideMap(popup);
  fireEvent.change(screen.getByLabelText("附近搜索"), { target: { value: "草稿" } });
  expect((screen.getByLabelText("附近搜索") as HTMLInputElement).value).toBe("草稿");
});

test("repositions after popup content and map size change without remounting, losing input or writing", async () => {
  const { popup, createMap, fetch, unmount } = await openPopover();
  fireEvent.change(screen.getByLabelText("附近搜索"), { target: { value: "附近草稿" } });
  Object.assign(geometry, { width: 700, height: 500, popupHeight: 180 });
  projection = { x: 650, y: 240 };
  act(() => observers.filter((observer) => [...observer.targets].some((node) => node.classList.contains("amap-base"))).forEach((observer) => observer.notify()));
  await waitFor(() => expectInsideMap(popup));
  geometry.popupHeight = 420;
  act(() => observers.filter((observer) => observer.targets.has(popup)).forEach((observer) => observer.notify()));
  await waitFor(() => expectInsideMap(popup));
  expect((screen.getByLabelText("附近搜索") as HTMLInputElement).value).toBe("附近草稿");
  expect(plannerStore.getSnapshot().selectedMapPoi?.id).toBe("poi_beihai");
  expect(createMap).toHaveBeenCalledTimes(1);
  expect(fetch.mock.calls.map(([url]) => String(url)).filter((url) => !url.includes("/map/config") && !url.includes("/map/pois?"))).toEqual([]);
  unmount();
  expect(observers.every((observer) => observer.targets.size === 0)).toBe(true);
});

test.each([900, -2000])("popup wheel %i keeps its input and selection without zooming the map", async (deltaY) => {
  const { popup, map, fetch } = await openPopover();
  const input = screen.getByLabelText("附近搜索");
  fireEvent.change(input, { target: { value: "滚动时保留草稿" } });
  input.focus();
  const requestCount = fetch.mock.calls.length;
  await act(async () => {
    fireEvent.wheel(input, { deltaY });
    await new Promise((resolve) => setTimeout(resolve, 180));
  });
  expect(map.zoomIn).not.toHaveBeenCalled();
  expect(map.zoomOut).not.toHaveBeenCalled();
  expect(popup.isConnected).toBe(true);
  expect(screen.getByLabelText("附近搜索")).toBe(input);
  expect((input as HTMLInputElement).value).toBe("滚动时保留草稿");
  expect(document.activeElement).toBe(input);
  expect(plannerStore.getSnapshot().selectedMapPoi?.id).toBe("poi_beihai");
  expect(fetch.mock.calls).toHaveLength(requestCount);
});

test.each([900, -2000])("map background wheel %i retains its existing zoom fallback", async (deltaY) => {
  const { container, map } = await openPopover();
  await act(async () => {
    fireEvent.wheel(container.querySelector(".amap-base")!, { deltaY });
    await new Promise((resolve) => setTimeout(resolve, 180));
  });
  expect(deltaY < 0 ? map.zoomIn : map.zoomOut).toHaveBeenCalledTimes(1);
  expect(deltaY < 0 ? map.zoomOut : map.zoomIn).not.toHaveBeenCalled();
});

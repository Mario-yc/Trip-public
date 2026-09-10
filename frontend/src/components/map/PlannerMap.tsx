import { FormEvent, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties, type WheelEvent } from "react";
import { ApiError, apiClient, ItineraryPlan, MapPoi, PendingPoiCandidate, PlannerPoi, PlannerSegment, type SpatialBoundaryPreview } from "../../services/apiClient";
import {
  plannerStore,
  type DensityMapCandidateChoice,
  type DensityMapComparisonAnchor
} from "../../state/plannerStore";
import { isCurrentVersionedWrite } from "../../state/versionedWriteGuard";
import {
  mapInteractionCapabilities,
  type MapInteractionCapabilities,
  type MapInteractionMode
} from "../../state/planComparisonPreview";
import { buildRouteLegColorMap, routeLegColor, routeLegKey } from "../timeline/routeVisuals";
import { isRouteAnchorSegment } from "../timeline/routeAnchors";
import { FocusDialog } from "../workspace/FocusDialog";

type PlannerMapProps = {
  plan: ItineraryPlan | null;
  selectedSegmentId: string | null;
  selectedDayNumber?: number | null;
  selectedRouteOptionId?: string | null;
  previewRouteOptionId?: string | null;
  onSelectSegment: (segmentId: string) => void;
  spatialBoundaryPreview?: SpatialBoundaryPreview | null;
  onConfirmDensityCandidate?: (choice: DensityMapCandidateChoice) => void | Promise<void>;
  city?: string;
  isActive?: boolean;
  interactionMode?: MapInteractionMode;
  routeColorOverrides?: Record<string, string>;
  routeOpacityOverrides?: Record<string, number>;
  segmentColorOverrides?: Record<string, string>;
  segmentOpacityOverrides?: Record<string, number>;
  comparisonLegend?: Array<{ id: string; label: string; color: string; focused: boolean }>;
  onFocusComparisonPlan?: (proposalId: string) => void;
  onDebugStateChange?: (state: MapInteractionDebugState) => void;
};

export type MapInteractionDebugState = {
  initialized: boolean;
  mapState: "idle" | "loading" | "ready" | "error";
  mode: MapInteractionMode;
  capabilities: MapInteractionCapabilities;
  center: [number, number] | null;
  zoom: number | null;
  interactionStartCenter: [number, number] | null;
  interactionStartZoom: number | null;
  dragCount: number;
  wheelCount: number;
  zoomCount: number;
  moveCount: number;
  recentEvents: Array<{ type: string; at: string }>;
  pointerTarget: string | null;
  updatedAt: string;
};

type AMapInstance = {
  destroy?: () => void;
  getZoom?: () => number;
  getCenter?: () => AMapLngLat;
  lngLatToContainer?: (lngLat: [number, number]) => AMapPixel;
  on?: (eventName: string, handler: (event?: AMapMapEvent) => void) => void;
  panTo?: (center: [number, number]) => void;
  setCenter?: (center: [number, number], immediately?: boolean, duration?: number) => void;
  zoomIn?: () => void;
  zoomOut?: () => void;
  setPitch?: (pitch: number) => void;
  setRotation?: (rotation: number) => void;
  setStatus?: (status: Record<string, boolean>) => void;
  setZoomAndCenter?: (zoom: number, center: [number, number], immediately?: boolean, duration?: number) => void;
  resize?: () => void;
};

type AMapOverlay = {
  setMap?: (map: AMapInstance | null) => void;
};

type AMapOverlayFactory = new (options: Record<string, unknown>) => AMapOverlay;

type MapMarkerPosition = {
  left: number;
  top: number;
};

type AMapPixel = {
  getX?: () => number;
  getY?: () => number;
  x?: number;
  y?: number;
};

type AMapLngLat = {
  getLng?: () => number;
  getLat?: () => number;
  lng?: number;
  lat?: number;
};

type AMapMapEvent = {
  lnglat?: AMapLngLat;
  poi?: {
    name?: string;
    location?: AMapLngLat;
  };
  pois?: Array<{
    name?: string;
    location?: AMapLngLat;
  }>;
};

type AMapRuntime = {
  Map: new (node: HTMLDivElement, options: Record<string, unknown>) => AMapInstance;
  Polyline: new (options: Record<string, unknown>) => AMapOverlay;
  Circle?: new (options: Record<string, unknown>) => AMapOverlay;
  Polygon?: new (options: Record<string, unknown>) => AMapOverlay;
};

type QuickCategory = "scenic" | "food" | "lodging" | "transport";
type RouteOption = ItineraryPlan["routeOptions"][number];
const EMPTY_ROUTE_STYLE_OVERRIDES: Record<string, never> = {};
const EMPTY_COMPARISON_LEGEND: Array<{ id: string; label: string; color: string; focused: boolean }> = [];
type RoutePolylinePart = {
  id: string;
  route: RouteOption;
  path: [number, number][];
  stepMode: string;
  stepIndex: number;
};

const CITY_CENTERS: Record<string, [number, number]> = {
  北京: [116.4074, 39.9042],
  北京市: [116.4074, 39.9042],
  上海: [121.4737, 31.2304],
  上海市: [121.4737, 31.2304],
  广州: [113.2644, 23.1291],
  广州市: [113.2644, 23.1291],
  深圳: [114.0579, 22.5431],
  深圳市: [114.0579, 22.5431]
};

const QUICK_CATEGORIES: Array<{ key: QuickCategory; label: string; keyword: string }> = [
  { key: "scenic", label: "景点", keyword: "景点" },
  { key: "food", label: "美食", keyword: "餐厅" },
  { key: "lodging", label: "住宿", keyword: "酒店" },
  { key: "transport", label: "交通", keyword: "交通" }
];
const NEARBY_RADIUS_OPTIONS = [500, 1000, 1500, 3000, 5000];
const MAX_NEARBY_RADIUS = NEARBY_RADIUS_OPTIONS[NEARBY_RADIUS_OPTIONS.length - 1];
const MAP_INTERACTION_MARKER_THROTTLE_MS = 80;
const MAX_MARKERS_TO_PROJECT_DURING_INTERACTION = 12;

export const isValidClosedBoundaryPath = (path: Array<[number, number]>): boolean => {
  if (path.length < 4 || path.length > 40) {
    return false;
  }
  if (
    path.some(
      ([longitude, latitude]) =>
        !Number.isFinite(longitude) ||
        !Number.isFinite(latitude) ||
        longitude < -180 ||
        longitude > 180 ||
        latitude < -90 ||
        latitude > 90
    )
  ) {
    return false;
  }
  const first = path[0];
  const last = path[path.length - 1];
  if (first[0] !== last[0] || first[1] !== last[1]) {
    return false;
  }
  const signedArea =
    path.slice(0, -1).reduce((area, point, index) => {
      const next = path[index + 1];
      return area + point[0] * next[1] - next[0] * point[1];
    }, 0) / 2;
  if (Math.abs(signedArea) < 1e-12) {
    return false;
  }
  const orientation = (a: [number, number], b: [number, number], c: [number, number]) =>
    (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]);
  const pointOnSegment = (
    point: [number, number],
    start: [number, number],
    end: [number, number]
  ) =>
    Math.abs(orientation(start, end, point)) <= 1e-12 &&
    Math.min(start[0], end[0]) <= point[0] &&
    point[0] <= Math.max(start[0], end[0]) &&
    Math.min(start[1], end[1]) <= point[1] &&
    point[1] <= Math.max(start[1], end[1]);
  const segmentsIntersect = (
    a: [number, number],
    b: [number, number],
    c: [number, number],
    d: [number, number]
  ) => {
    const o1 = orientation(a, b, c);
    const o2 = orientation(a, b, d);
    const o3 = orientation(c, d, a);
    const o4 = orientation(c, d, b);
    if ((o1 > 1e-12) !== (o2 > 1e-12) && (o3 > 1e-12) !== (o4 > 1e-12)) {
      return true;
    }
    return (
      (Math.abs(o1) <= 1e-12 && pointOnSegment(c, a, b)) ||
      (Math.abs(o2) <= 1e-12 && pointOnSegment(d, a, b)) ||
      (Math.abs(o3) <= 1e-12 && pointOnSegment(a, c, d)) ||
      (Math.abs(o4) <= 1e-12 && pointOnSegment(b, c, d))
    );
  };
  for (let left = 0; left < path.length - 1; left += 1) {
    for (let right = left + 1; right < path.length - 1; right += 1) {
      if (Math.abs(left - right) <= 1 || (left === 0 && right === path.length - 2)) {
        continue;
      }
      if (segmentsIntersect(path[left], path[left + 1], path[right], path[right + 1])) {
        return false;
      }
    }
  }
  return true;
};

const MAP_RUNTIME_SLOT_LIMIT = 10;
const BASIC_SEARCH_SLOT_LIMIT = 3;

type MapRuntimeSlotRelease = () => void;
type MapRuntimeSlotRequest = { acquire: () => void; cancelled: boolean };

let mapRuntimeActiveSlots = 0;
const mapRuntimeWaitQueue: MapRuntimeSlotRequest[] = [];
let basicSearchActiveSlots = 0;
const basicSearchWaitQueue: MapRuntimeSlotRequest[] = [];

function releaseNextMapRuntimeSlot() {
  let next = mapRuntimeWaitQueue.shift();
  while (next?.cancelled) {
    next = mapRuntimeWaitQueue.shift();
  }
  next?.acquire();
}

const acquireMapRuntimeSlot = (signal?: AbortSignal): Promise<MapRuntimeSlotRelease | null> =>
  new Promise((resolve) => {
    if (signal?.aborted) {
      resolve(null);
      return;
    }

    let settled = false;
    let request: MapRuntimeSlotRequest | null = null;
    const settle = (release: MapRuntimeSlotRelease | null) => {
      if (settled) {
        return;
      }
      settled = true;
      signal?.removeEventListener("abort", cancelQueuedRequest);
      resolve(release);
    };
    const cancelQueuedRequest = () => {
      if (request) {
        request.cancelled = true;
      }
      settle(null);
    };
    const acquire = () => {
      if (signal?.aborted || request?.cancelled) {
        settle(null);
        return;
      }
      mapRuntimeActiveSlots += 1;
      let released = false;
      settle(() => {
        if (released) {
          return;
        }
        released = true;
        mapRuntimeActiveSlots = Math.max(0, mapRuntimeActiveSlots - 1);
        releaseNextMapRuntimeSlot();
      });
    };

    signal?.addEventListener("abort", cancelQueuedRequest, { once: true });
    if (mapRuntimeActiveSlots < MAP_RUNTIME_SLOT_LIMIT) {
      acquire();
      return;
    }
    request = { acquire, cancelled: false };
    mapRuntimeWaitQueue.push(request);
  });

function releaseNextBasicSearchSlot() {
  let next = basicSearchWaitQueue.shift();
  while (next?.cancelled) {
    next = basicSearchWaitQueue.shift();
  }
  next?.acquire();
}

const acquireBasicSearchSlot = (): Promise<MapRuntimeSlotRelease> =>
  new Promise((resolve) => {
    let released = false;
    const release = () => {
      if (released) {
        return;
      }
      released = true;
      basicSearchActiveSlots = Math.max(0, basicSearchActiveSlots - 1);
      releaseNextBasicSearchSlot();
    };
    const acquire = () => {
      basicSearchActiveSlots += 1;
      resolve(release);
    };

    if (basicSearchActiveSlots < BASIC_SEARCH_SLOT_LIMIT) {
      acquire();
      return;
    }
    basicSearchWaitQueue.push({ acquire, cancelled: false });
  });
export function PlannerMap({
  plan,
  selectedSegmentId,
  selectedDayNumber,
  selectedRouteOptionId,
  previewRouteOptionId,
  onSelectSegment,
  spatialBoundaryPreview = null,
  onConfirmDensityCandidate,
  city = "",
  isActive = true,
  interactionMode = "itinerary_edit",
  routeColorOverrides = EMPTY_ROUTE_STYLE_OVERRIDES,
  routeOpacityOverrides = EMPTY_ROUTE_STYLE_OVERRIDES,
  segmentColorOverrides = EMPTY_ROUTE_STYLE_OVERRIDES,
  segmentOpacityOverrides = EMPTY_ROUTE_STYLE_OVERRIDES,
  comparisonLegend = EMPTY_COMPARISON_LEGEND,
  onFocusComparisonPlan,
  onDebugStateChange
}: PlannerMapProps) {
  const capabilities = useMemo(() => mapInteractionCapabilities(interactionMode), [interactionMode]);
  const readOnlyMap = !capabilities.mutateItinerary;
  const showCandidatePoiResults = capabilities.search || capabilities.confirmPendingSlot;
  const mapNode = useRef<HTMLDivElement | null>(null);
  const mapInstance = useRef<AMapInstance | null>(null);
  const amapRuntime = useRef<AMapRuntime | null>(null);
  const capabilitiesRef = useRef(capabilities);
  const onDebugStateChangeRef = useRef(onDebugStateChange);
  const mapDebugStateRef = useRef<MapInteractionDebugState>({
    initialized: false,
    mapState: "idle",
    mode: interactionMode,
    capabilities,
    center: null,
    zoom: null,
    interactionStartCenter: null,
    interactionStartZoom: null,
    dragCount: 0,
    wheelCount: 0,
    zoomCount: 0,
    moveCount: 0,
    recentEvents: [],
    pointerTarget: null,
    updatedAt: new Date().toISOString()
  });
  const selectedRoutePolylines = useRef<Map<string, AMapOverlay[]>>(new Map());
  const previewRoutePolylines = useRef<AMapOverlay[]>([]);
  const nearbyCircle = useRef<AMapOverlay | null>(null);
  const spatialBoundaryPolygon = useRef<AMapOverlay | null>(null);
  const nearbyBaseline = useRef<{ pois: MapPoi[]; selectedPoiId: string | null } | null>(null);
  const didInitialFitView = useRef(false);
  const allowInitialFitView = useRef(false);
  const lastComparisonViewportKey = useRef("");
  const suppressNextMoveClose = useRef(false);
  const selectedPoiRef = useRef<MapPoi | null>(null);
  const selectedSegmentIdRef = useRef<string | null>(selectedSegmentId);
  const selectedTimelinePoiRef = useRef<MapPoi | null>(null);
  const onSelectSegmentRef = useRef(onSelectSegment);
  const identifyPoiFromMapClickRef = useRef<(event?: AMapMapEvent) => void>(() => undefined);
  const popoverFrame = useRef<number | null>(null);
  const markerFrame = useRef<number | null>(null);
  const markerElements = useRef<Map<string, HTMLButtonElement>>(new Map());
  const latestPopoverPosition = useRef<{ left: number; top: number } | null>(null);
  const mapInteractingRef = useRef(false);
  const lastMarkerProjectionAt = useRef(0);
  const closedOverlayDuringInteraction = useRef(false);
  const mapSearchRequestSeq = useRef(0);
  const poiReplacementRequestSeq = useRef(0);
  const selectedPoiIdRef = useRef<string | null>(null);
  const collapsedPopoverPoiIdRef = useRef<string | null>(null);
  const segmentsRef = useRef<PlannerSegment[]>([]);
  const poiResultsRef = useRef<MapPoi[]>([]);
  const densityAnchorsRef = useRef<DensityMapComparisonAnchor[]>([]);
  const previousDensityCandidateRecordId = useRef<string | null>(null);
  const [mapState, setMapState] = useState<"idle" | "loading" | "ready" | "error">("idle");
  const [mapLoadAttempt, setMapLoadAttempt] = useState(0);
  const [mapError, setMapError] = useState("");
  const [searchKeyword, setSearchKeyword] = useState("");
  const [activeCategory, setActiveCategory] = useState<QuickCategory | "all">("all");
  const [poiResults, setPoiResults] = useState<MapPoi[]>([]);
  const visiblePoiResults = useMemo(
    () => (showCandidatePoiResults ? poiResults : []),
    [poiResults, showCandidatePoiResults]
  );
  const [selectedPoiId, setSelectedPoiId] = useState<string | null>(null);
  const [poiSearchState, setPoiSearchState] = useState<"idle" | "loading" | "ready" | "error">("idle");
  const [poiSearchError, setPoiSearchError] = useState("");
  const [nearbyKeyword, setNearbyKeyword] = useState("");
  const [nearbyRadius, setNearbyRadius] = useState(1500);
  const [nearbyActive, setNearbyActive] = useState(false);
  const [collapsedPopoverPoiId, setCollapsedPopoverPoiId] = useState<string | null>(null);
  const [dismissedTimelinePopoverSegmentId, setDismissedTimelinePopoverSegmentId] = useState<string | null>(null);
  const [poiResolveState, setPoiResolveState] = useState<"idle" | "loading" | "accepted" | "pending" | "error">("idle");
  const [poiResolveMessage, setPoiResolveMessage] = useState("");
  const [poiAddState, setPoiAddState] = useState<"idle" | "saving" | "saved" | "error">("idle");
  const [poiAddMessage, setPoiAddMessage] = useState("");
  const [poiAddMessageTargetId, setPoiAddMessageTargetId] = useState<string | null>(null);
  const [photoViewerOpen, setPhotoViewerOpen] = useState(false);
  const poiKeyboardOpener = useRef<HTMLElement | null>(null);
  const poiPopoverRef = useRef<HTMLDivElement | null>(null);
  const [photoIndex, setPhotoIndex] = useState(0);
  const [popoverPosition, setPopoverPosition] = useState<{ left: number; top: number } | null>(null);
  const [mapInteracting, setMapInteracting] = useState(false);
  const [plannerSnapshot, setPlannerSnapshot] = useState(plannerStore.getSnapshot());
  const densityComparison =
    plannerSnapshot.activeDensityMapComparison?.sessionId ===
    plannerSnapshot.agentSession?.sessionId
      ? plannerSnapshot.activeDensityMapComparison
      : null;
  const publishMapDebug = useCallback(
    (
      eventType?: string,
      patch: Partial<MapInteractionDebugState> = {},
      counter?: "dragCount" | "wheelCount" | "zoomCount" | "moveCount"
    ) => {
      const activeMap = mapInstance.current;
      let center = mapDebugStateRef.current.center;
      let zoom = mapDebugStateRef.current.zoom;
      try {
        center = lngLatToTuple(activeMap?.getCenter?.()) ?? center;
        const currentZoom = activeMap?.getZoom?.();
        zoom = typeof currentZoom === "number" && Number.isFinite(currentZoom) ? currentZoom : zoom;
      } catch {
        // Keep the last known viewport in diagnostic state.
      }
      const current = mapDebugStateRef.current;
      const next: MapInteractionDebugState = {
        ...current,
        ...(counter && current.interactionStartCenter === null
          ? { interactionStartCenter: center, interactionStartZoom: zoom }
          : {}),
        ...patch,
        center,
        zoom,
        recentEvents: eventType
          ? [...current.recentEvents, { type: eventType, at: new Date().toISOString() }].slice(-16)
          : current.recentEvents,
        updatedAt: new Date().toISOString()
      };
      if (counter) {
        next[counter] = current[counter] + 1;
      }
      mapDebugStateRef.current = next;
      onDebugStateChangeRef.current?.(next);
    },
    []
  );

  useEffect(() => {
    capabilitiesRef.current = capabilities;
    onDebugStateChangeRef.current = onDebugStateChange;
    mapInstance.current?.setStatus?.({
      dragEnable: capabilities.navigate,
      zoomEnable: capabilities.navigate,
      scrollWheel: capabilities.navigate,
      doubleClickZoom: capabilities.navigate
    });
    publishMapDebug("mode_changed", { mode: interactionMode, capabilities });
  }, [capabilities, interactionMode, onDebugStateChange, publishMapDebug]);

  useEffect(() => {
    publishMapDebug(`map_state_${mapState}`, { mapState, initialized: mapState === "ready" });
  }, [mapState, publishMapDebug]);
  const densityComparisonAnchors = useMemo(
    () => densityComparison?.anchors ?? [],
    [densityComparison]
  );
  const displayDays = useMemo(() => displayMapDays(plan, selectedDayNumber), [plan, selectedDayNumber]);
  const segments = useMemo(() => displayDays.flatMap((day) => day.segments), [displayDays]);
  const comparisonViewportKey = useMemo(
    () =>
      interactionMode === "itinerary_edit"
        ? ""
        : [
            interactionMode,
            plan?.id ?? "no-plan",
            selectedDayNumber ?? "all-days",
            ...segments.map((segment) => segment.id)
          ].join(":"),
    [interactionMode, plan?.id, segments, selectedDayNumber]
  );
  const routeOptions = useMemo(() => plan?.routeOptions ?? [], [plan?.routeOptions]);
  const routeColorMap = useMemo(() => buildRouteLegColorMap(plan?.days ?? [], routeOptions), [plan?.days, routeOptions]);
  const selectedIndex = Math.max(
    0,
    segments.findIndex((segment) => segment.id === selectedSegmentId)
  );
  const selected = segments[selectedIndex] ?? segments[0] ?? null;
  const selectedRoutes = useMemo(() => mapDisplayRoutes(displayDays, routeOptions), [displayDays, routeOptions]);
  const summaryRoutes = selectedRoutes;
  const previewRoute = routeOptions.find((item) => item.id === previewRouteOptionId && item.polyline?.length) ?? null;
  const routeSummaries = summaryRoutes;
  const routeSegmentsById = useMemo(() => new Map(segments.map((segment) => [segment.id, segment])), [segments]);
  const totalDistance = summaryRoutes.reduce((sum, item) => sum + item.distanceMeters, 0);
  const totalDuration = summaryRoutes.reduce((sum, item) => sum + item.durationMinutes, 0);
  const mapPoints = useMemo(
    () =>
      segments.flatMap((segment) =>
        isMappableSegment(segment) ? ([[segment.poi.longitude, segment.poi.latitude] as [number, number]] as const) : []
      ),
    [segments]
  );
  const densityComparisonPoints = useMemo(
    () =>
      densityComparison
        ? [...densityComparisonAnchors, ...visiblePoiResults].flatMap((poi) =>
            Number.isFinite(poi.longitude) && Number.isFinite(poi.latitude)
              ? ([[poi.longitude, poi.latitude] as [number, number]] as const)
              : []
          )
        : [],
    [densityComparison, densityComparisonAnchors, visiblePoiResults]
  );
  const densityComparisonFitKey = densityComparison
    ? `${densityComparison.candidateRecordId}:${densityComparisonPoints
        .map(([longitude, latitude]) => `${longitude},${latitude}`)
        .join("|")}`
    : "";
  const lastDensityComparisonFitKey = useRef("");
  const center = CITY_CENTERS[city];
  const centerRef = useRef(center);
  const mapPointsLengthRef = useRef(mapPoints.length);
  const mapContextKey = `${plannerSnapshot.agentSession?.sessionId ?? "__no_agent_session__"}:${plan?.id ?? "__no_plan__"}`;
  const mapRequestContextKey = `${mapContextKey}:${selectedSegmentId ?? "__no_selected_segment__"}`;
  const currentMapRequestContextKey = useRef(mapRequestContextKey);
  const previousMapContextKey = useRef(mapContextKey);
  const suppressNextPoiStoreSync = useRef(false);
  const lastTimelineSelectionRequestId = useRef(plannerSnapshot.timelineSelectionRequestId);
  const selectedTimelinePoi = useMemo(
    () => (selectedSegmentId && selected?.id === selectedSegmentId && isMappableSegment(selected) ? mapPoiFromSegment(selected) : null),
    [selected, selectedSegmentId]
  );
  const candidateSelectedPoi = visiblePoiResults.find((poi) => poi.id === selectedPoiId) ?? null;
  const densityAnchorSelectedPoi =
    densityComparisonAnchors.find((poi) => poi.id === selectedPoiId) ?? null;
  const selectedPoi =
    candidateSelectedPoi ??
    densityAnchorSelectedPoi ??
    (selectedTimelinePoi && dismissedTimelinePopoverSegmentId !== selectedSegmentId ? selectedTimelinePoi : null);
  const selectedDensityCandidateChoice = selectedPoi && densityComparison
    ? (densityComparison.candidateChoices ?? []).find(
        (choice) => choice.amapId === String(selectedPoi.amapId || selectedPoi.id)
      ) ?? null
    : null;
  const selectedPoiIsTimeline = Boolean(selectedPoi && selectedTimelinePoi && selectedPoi.id === selectedTimelinePoi.id && !candidateSelectedPoi);
  const selectedPoiPopoverOpen = Boolean(selectedPoi && collapsedPopoverPoiId !== selectedPoi.id);
  const selectedPhoto = selectedPoi?.photos[photoIndex] ?? selectedPoi?.photos[0] ?? null;
  // Replacement feedback belongs to the transaction, not the transient map popover.
  const visiblePoiAddMessage = poiAddMessage;
  const popoverPoiAddMessage = poiAddMessage && selectedPoi?.id === poiAddMessageTargetId ? poiAddMessage : "";

  centerRef.current = center;
  mapPointsLengthRef.current = mapPoints.length;
  onSelectSegmentRef.current = onSelectSegment;
  currentMapRequestContextKey.current = mapRequestContextKey;
  selectedSegmentIdRef.current = selectedSegmentId;
  selectedTimelinePoiRef.current = selectedTimelinePoi;
  selectedPoiIdRef.current = selectedPoiId;
  collapsedPopoverPoiIdRef.current = collapsedPopoverPoiId;
  segmentsRef.current = segments;
  poiResultsRef.current = visiblePoiResults;
  densityAnchorsRef.current = densityComparisonAnchors;

  const setPopoverPositionIfChanged = useCallback((nextPosition: { left: number; top: number } | null) => {
    const map = mapNode.current;
    const popup = poiPopoverRef.current;
    if (map && popup && map.clientWidth > 0 && map.clientHeight > 0 && popup.offsetWidth > 0 && popup.offsetHeight > 0) {
      // The popup's CSS translates this anchor by (-50%, -100% - 12px).
      // Keep the whole popup inside the map, including before SDK projection is ready.
      const inset = 8;
      const gap = 12;
      const anchor = nextPosition ?? { left: map.clientWidth / 2, top: map.clientHeight / 2 };
      nextPosition = {
        left: Math.max(popup.offsetWidth / 2 + inset, Math.min(anchor.left, map.clientWidth - popup.offsetWidth / 2 - inset)),
        top: Math.max(popup.offsetHeight + gap + inset, Math.min(anchor.top, map.clientHeight + gap - inset))
      };
    }
    const previous = latestPopoverPosition.current;
    const changed =
      (!previous && Boolean(nextPosition)) ||
      (previous && !nextPosition) ||
      (previous && nextPosition && (Math.abs(previous.left - nextPosition.left) >= 1 || Math.abs(previous.top - nextPosition.top) >= 1));
    if (!changed) {
      return;
    }
    latestPopoverPosition.current = nextPosition;
    setPopoverPosition(nextPosition);
  }, []);

  const containerPositionForCoordinates = useCallback((position: [number, number]) => {
    let pixel: AMapPixel | undefined;
    try {
      pixel = mapInstance.current?.lngLatToContainer?.(position);
    } catch (error) {
      console.warn("AMap lngLatToContainer failed", error);
      return null;
    }
    const left = typeof pixel?.getX === "function" ? pixel.getX() : pixel?.x;
    const top = typeof pixel?.getY === "function" ? pixel.getY() : pixel?.y;
    if (typeof left !== "number" || typeof top !== "number") {
      return null;
    }
    return { left, top };
  }, []);

  const containerPositionForPoi = useCallback((poi: MapPoi) => (
    containerPositionForCoordinates([poi.longitude, poi.latitude])
  ), [containerPositionForCoordinates]);

  const applyDomMarkerPosition = useCallback((markerKey: string, position: MapMarkerPosition | null) => {
    const marker = markerElements.current.get(markerKey);
    if (!marker) {
      return;
    }
    if (!position) {
      marker.style.visibility = "hidden";
      marker.dataset.projected = "false";
      return;
    }
    marker.style.visibility = "visible";
    marker.style.transform = `translate3d(${position.left}px, ${position.top}px, 0) translate(-50%, -100%)`;
    marker.dataset.projected = "true";
  }, []);

  const updateSelectedPopoverPosition = useCallback(() => {
    const poi = selectedPoiRef.current;
    if (!poi) {
      return;
    }
    setPopoverPositionIfChanged(containerPositionForPoi(poi));
  }, [containerPositionForPoi, setPopoverPositionIfChanged]);

  const scheduleSelectedPopoverPosition = useCallback(() => {
    if (popoverFrame.current !== null) {
      return;
    }
    const schedule = window.requestAnimationFrame ?? ((callback: FrameRequestCallback) => window.setTimeout(() => callback(Date.now()), 16));
    popoverFrame.current = schedule(() => {
      popoverFrame.current = null;
      updateSelectedPopoverPosition();
    });
  }, [updateSelectedPopoverPosition]);

  const cancelPopoverFrame = useCallback(() => {
    if (popoverFrame.current === null) {
      return;
    }
    const cancel = window.cancelAnimationFrame ?? window.clearTimeout;
    cancel(popoverFrame.current);
    popoverFrame.current = null;
  }, []);

  useLayoutEffect(() => {
    const popup = poiPopoverRef.current;
    if (!popup || !selectedPoi || !selectedPoiPopoverOpen) return;
    setPopoverPositionIfChanged(containerPositionForPoi(selectedPoi));
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(scheduleSelectedPopoverPosition);
    observer.observe(popup);
    return () => observer.disconnect();
  }, [selectedPoi, selectedPoiPopoverOpen, containerPositionForPoi, setPopoverPositionIfChanged, scheduleSelectedPopoverPosition]);

  const updateMapMarkerPositions = useCallback(() => {
    const projectedKeys = new Set<string>();
    for (const segment of segmentsRef.current) {
      if (!isMappableSegment(segment)) {
        continue;
      }
      const markerKey = `segment:${segment.id}`;
      const position = containerPositionForCoordinates([segment.poi.longitude, segment.poi.latitude]);
      applyDomMarkerPosition(markerKey, position);
      projectedKeys.add(markerKey);
    }
    for (const poi of poiResultsRef.current) {
      const markerKey = `poi:${poi.id}`;
      const position = containerPositionForCoordinates([poi.longitude, poi.latitude]);
      applyDomMarkerPosition(markerKey, position);
      projectedKeys.add(markerKey);
    }
    for (const anchor of densityAnchorsRef.current) {
      const markerKey = `density-anchor:${anchor.id}`;
      const position = containerPositionForCoordinates([
        anchor.longitude,
        anchor.latitude
      ]);
      applyDomMarkerPosition(markerKey, position);
      projectedKeys.add(markerKey);
    }
    markerElements.current.forEach((_marker, markerKey) => {
      if (!projectedKeys.has(markerKey)) {
        applyDomMarkerPosition(markerKey, null);
      }
    });
  }, [applyDomMarkerPosition, containerPositionForCoordinates]);

  const scheduleMapMarkerPositions = useCallback((options?: { force?: boolean }) => {
    if (!options?.force && mapInteractingRef.current) {
      const markerCount =
        segmentsRef.current.filter(isMappableSegment).length +
        poiResultsRef.current.length +
        densityAnchorsRef.current.length;
      if (markerCount > MAX_MARKERS_TO_PROJECT_DURING_INTERACTION) {
        return;
      }
      const now = (typeof performance !== "undefined" ? performance.now() : Date.now());
      if (lastMarkerProjectionAt.current > 0 && now - lastMarkerProjectionAt.current < MAP_INTERACTION_MARKER_THROTTLE_MS) {
        return;
      }
      lastMarkerProjectionAt.current = now;
    }
    if (markerFrame.current !== null) {
      return;
    }
    const schedule = window.requestAnimationFrame ?? ((callback: FrameRequestCallback) => window.setTimeout(() => callback(Date.now()), 16));
    markerFrame.current = schedule(() => {
      markerFrame.current = null;
      updateMapMarkerPositions();
    });
  }, [updateMapMarkerPositions]);

  const cancelMarkerFrame = useCallback(() => {
    if (markerFrame.current === null) {
      return;
    }
    const cancel = window.cancelAnimationFrame ?? window.clearTimeout;
    cancel(markerFrame.current);
    markerFrame.current = null;
  }, []);

  const clearNearbyCircle = useCallback(() => {
    removeMapOverlays(nearbyCircle.current ? [nearbyCircle.current] : []);
    nearbyCircle.current = null;
  }, []);

  const drawNearbyCircle = useCallback((poi: MapPoi, radius: number) => {
    clearNearbyCircle();
    if (!amapRuntime.current?.Circle || !mapInstance.current) {
      setPoiAddState("error");
      setPoiAddMessage("当前高德地图运行时不支持范围圈展示，附近搜索结果仍可查看。");
      setPoiAddMessageTargetId(poi.id);
      return;
    }
    const circle = createMapOverlay(amapRuntime.current.Circle, {
      center: [poi.longitude, poi.latitude],
      radius,
      strokeColor: "#2f6fed",
      strokeOpacity: 0.85,
      strokeWeight: 2,
      strokeStyle: "solid",
      fillColor: "#2f6fed",
      fillOpacity: 0.04,
      bubble: true,
      zIndex: 80
    }, mapInstance.current, "Circle");
    if (!circle) {
      setPoiAddState("error");
      setPoiAddMessage("范围圈暂时无法显示，附近搜索结果仍可查看。");
      setPoiAddMessageTargetId(poi.id);
      return;
    }
    nearbyCircle.current = circle;
  }, [clearNearbyCircle]);

  const showNearbyRadiusCircle = useCallback(() => {
    if (!selectedPoi) {
      return;
    }
    drawNearbyCircle(selectedPoi, nearbyRadius);
  }, [drawNearbyCircle, nearbyRadius, selectedPoi]);

  const closePoiOverlays = useCallback((options?: { syncStore?: boolean }) => {
    setPhotoViewerOpen(false);
    setSelectedPoiId(null);
    setCollapsedPopoverPoiId(null);
    setNearbyKeyword("");
    latestPopoverPosition.current = null;
    setPopoverPosition(null);
    if (options?.syncStore === false) {
      const snapshot = plannerStore.getSnapshot();
      if (snapshot.selectedMapPoi) {
        closedOverlayDuringInteraction.current = true;
      }
      return;
    }
    plannerStore.setState({ selectedMapPoi: null });
  }, []);

  const dismissPoiOverlays = useCallback((options?: { syncStore?: boolean }) => {
    const segmentId = selectedSegmentIdRef.current;
    if (selectedTimelinePoiRef.current && segmentId) {
      setDismissedTimelinePopoverSegmentId(segmentId);
    }
    closePoiOverlays(options);
  }, [closePoiOverlays]);

  const clearStoredSelectedMapPoi = useCallback(() => {
    const snapshot = plannerStore.getSnapshot();
    if (!snapshot.selectedMapPoi) {
      return;
    }
    plannerStore.setState({ selectedMapPoi: null });
  }, []);

  const selectTimelineSegmentOnMap = useCallback((segmentId: string) => {
    setDismissedTimelinePopoverSegmentId(null);
    selectedPoiIdRef.current = null;
    setSelectedPoiId(null);
    clearStoredSelectedMapPoi();
    onSelectSegment(segmentId);
  }, [clearStoredSelectedMapPoi, onSelectSegment]);

  const cancelNearbySearch = useCallback(() => {
    const baseline = nearbyBaseline.current;
    setPoiResults(baseline?.pois ?? []);
    setSelectedPoiId(baseline?.selectedPoiId ?? null);
    setNearbyActive(false);
    nearbyBaseline.current = null;
    clearNearbyCircle();
    setPoiSearchState(baseline?.pois.length ? "ready" : "idle");
    setPoiSearchError("");
    setPoiAddState("idle");
    setPoiAddMessage("");
    setPoiAddMessageTargetId(null);
    if (!baseline?.selectedPoiId) {
      plannerStore.setState({ selectedMapPoi: null });
    }
  }, [clearNearbyCircle]);

  const clearPoiSearchResults = useCallback(() => {
    setPoiResults([]);
    setSelectedPoiId(null);
    setSearchKeyword("");
    setActiveCategory("all");
    setNearbyActive(false);
    nearbyBaseline.current = null;
    clearNearbyCircle();
    setPoiSearchState("idle");
    setPoiSearchError("");
    setPoiResolveState("idle");
    setPoiResolveMessage("");
    setPoiAddState("idle");
    setPoiAddMessage("");
    setPoiAddMessageTargetId(null);
    closePoiOverlays();
    plannerStore.setState({
      candidateMapPois: [],
      selectedMapPoi: null
    });
  }, [clearNearbyCircle, closePoiOverlays]);

  const resolveCurrentLocation = useCallback(async (options?: { useSlot?: boolean; signal?: AbortSignal }): Promise<[number, number] | null> => {
    const shouldAcquireSlot = options?.useSlot ?? true;
    let releaseSlot: (() => void) | null = null;
    if (shouldAcquireSlot) {
      releaseSlot = await acquireMapRuntimeSlot(options?.signal);
    }
    try {
      if (!releaseSlot && shouldAcquireSlot) {
        return null;
      }
      if (options?.signal?.aborted) {
        return null;
      }
      if (typeof navigator === "undefined" || !navigator.geolocation) {
        return null;
      }

      return await new Promise((resolve) => {
        navigator.geolocation.getCurrentPosition(
          (position) => resolve(options?.signal?.aborted ? null : [position.coords.longitude, position.coords.latitude]),
          () => resolve(null),
          {
            enableHighAccuracy: false,
            timeout: 6000,
            maximumAge: 300000,
          }
        );
      });
    } finally {
      releaseSlot?.();
    }
  }, []);
  const moveMapTo = useCallback((position: [number, number]) => {
    const map = mapInstance.current;
    if (!map) {
      return;
    }
    try {
      suppressNextMoveClose.current = true;
      if (typeof map.setCenter === "function") {
        map.setCenter(position, true, 0);
      } else if (typeof map.setZoomAndCenter === "function") {
        map.setZoomAndCenter(map.getZoom?.() ?? 12, position, true, 0);
      } else if (typeof map.panTo === "function") {
        map.panTo(position);
      }
    } catch (error) {
      suppressNextMoveClose.current = false;
      console.warn("AMap move command failed", error);
      return;
    }
    scheduleSelectedPopoverPosition();
    scheduleMapMarkerPositions();
  }, [scheduleMapMarkerPositions, scheduleSelectedPopoverPosition]);

  const focusPoi = useCallback((poi: MapPoi) => {
    moveMapTo([poi.longitude, poi.latitude]);
  }, [moveMapTo]);

  const beginMapInteraction = useCallback(() => {
    mapInteractingRef.current = true;
    setMapInteracting(true);
    dismissPoiOverlays({ syncStore: false });
  }, [dismissPoiOverlays]);

  const handleMapMoveStart = useCallback(() => {
    publishMapDebug("movestart", {}, "moveCount");
    if (suppressNextMoveClose.current) {
      suppressNextMoveClose.current = false;
      return;
    }
    beginMapInteraction();
  }, [beginMapInteraction, publishMapDebug]);

  const handleUserMapDrag = useCallback(() => {
    publishMapDebug("dragstart", {}, "dragCount");
    suppressNextMoveClose.current = false;
    beginMapInteraction();
  }, [beginMapInteraction, publishMapDebug]);

  const handleBaseMapClick = useCallback((event?: AMapMapEvent) => {
    publishMapDebug("map_click");
    if (densityComparison || !capabilitiesRef.current.search) {
      return;
    }
    identifyPoiFromMapClickRef.current(event);
  }, [densityComparison, publishMapDebug]);

  const handleUserMapZoomStart = useCallback(() => {
    publishMapDebug("zoomstart", {}, "zoomCount");
    suppressNextMoveClose.current = false;
    beginMapInteraction();
  }, [beginMapInteraction, publishMapDebug]);

  const handleMapWheel = useCallback((event: WheelEvent<HTMLDivElement>) => {
    // Wheel input inside the popup belongs to its scrollable content, not map navigation.
    if (event.target instanceof Node && poiPopoverRef.current?.contains(event.target)) return;
    publishMapDebug("wheel", {}, "wheelCount");
    const activeMap = mapInstance.current;
    if (!capabilitiesRef.current.navigate || !activeMap) return;
    const zoomBefore = activeMap.getZoom?.();
    const direction = event.deltaY < 0 ? "in" : "out";
    window.setTimeout(() => {
      const currentMap = mapInstance.current;
      const zoomAfter = currentMap?.getZoom?.();
      if (
        !currentMap ||
        !capabilitiesRef.current.navigate ||
        typeof zoomBefore !== "number" ||
        typeof zoomAfter !== "number" ||
        Math.abs(zoomAfter - zoomBefore) > 1e-7
      ) {
        return;
      }
      if (direction === "in") currentMap.zoomIn?.();
      else currentMap.zoomOut?.();
      publishMapDebug("wheel_zoom_fallback");
    }, 120);
  }, [publishMapDebug]);

  const handlePoiSelect = useCallback((poi: MapPoi) => {
    if (selectedPoiIdRef.current === poi.id) {
      const shouldCollapse = collapsedPopoverPoiIdRef.current !== poi.id;
      setPhotoViewerOpen(false);
      collapsedPopoverPoiIdRef.current = shouldCollapse ? poi.id : null;
      setCollapsedPopoverPoiId(shouldCollapse ? poi.id : null);
      if (shouldCollapse) {
        latestPopoverPosition.current = null;
        setPopoverPosition(null);
      } else {
        focusPoi(poi);
        setPopoverPositionIfChanged(containerPositionForPoi(poi));
      }
      return;
    }
    setDismissedTimelinePopoverSegmentId(null);
    setCollapsedPopoverPoiId(null);
    collapsedPopoverPoiIdRef.current = null;
    selectedPoiIdRef.current = poi.id;
    setSelectedPoiId(poi.id);
    setPhotoIndex(0);
    setPhotoViewerOpen(false);
    setNearbyKeyword("");
    setNearbyActive(false);
    nearbyBaseline.current = null;
    clearNearbyCircle();
    setPoiResolveState("idle");
    setPoiResolveMessage("");
    setPoiAddState("idle");
    setPoiAddMessage("");
    setPoiAddMessageTargetId(null);
    plannerStore.setState({ selectedMapPoi: poi });
    focusPoi(poi);
    setPopoverPositionIfChanged(containerPositionForPoi(poi));
  }, [clearNearbyCircle, containerPositionForPoi, focusPoi, setPopoverPositionIfChanged]);

  const syncSelectedRoutePolylines = useCallback((amap: AMapRuntime, map: AMapInstance) => {
    selectedRoutePolylines.current.forEach((polylines) => removeMapOverlays(polylines));
    selectedRoutePolylines.current = new Map();
    for (const part of routePolylineParts(selectedRoutes)) {
      const isHighlighted = isHighlightedRoute(part.route, selectedRouteOptionId);
      const polyline = createMapOverlay(amap.Polyline, {
        path: part.path,
        ...routePolylineStyle(part, isHighlighted, false, routeColorOverrides[part.route.id] ?? routeLegColor(part.route, routeColorMap)),
        ...(routeOpacityOverrides[part.route.id] !== undefined ? { strokeOpacity: routeOpacityOverrides[part.route.id] } : {})
      }, map, "Polyline");
      if (polyline) {
        selectedRoutePolylines.current.set(part.route.id, [...(selectedRoutePolylines.current.get(part.route.id) ?? []), polyline]);
      }
    }
  }, [routeColorMap, routeColorOverrides, routeOpacityOverrides, selectedRouteOptionId, selectedRoutes]);

  const syncPreviewRoutePolyline = useCallback((amap: AMapRuntime, map: AMapInstance) => {
    removeMapOverlays(previewRoutePolylines.current);
    previewRoutePolylines.current = [];
    if (previewRoute) {
      previewRoutePolylines.current = routePolylineParts([previewRoute])
        .map((part) => createMapOverlay(amap.Polyline, {
          path: part.path,
          ...routePolylineStyle(part, true, true, routeLegColor(part.route, routeColorMap))
        }, map, "Polyline"))
        .filter((overlay): overlay is AMapOverlay => Boolean(overlay));
    }
  }, [previewRoute, routeColorMap]);

  const handleMapViewportSettled = useCallback(() => {
    publishMapDebug("viewport_settled");
    mapInteractingRef.current = false;
    lastMarkerProjectionAt.current = 0;
    setMapInteracting(false);
    if (closedOverlayDuringInteraction.current) {
      closedOverlayDuringInteraction.current = false;
      window.setTimeout(() => {
        plannerStore.setState({ selectedMapPoi: null });
      }, 0);
    }
    scheduleSelectedPopoverPosition();
    scheduleMapMarkerPositions({ force: true });
  }, [publishMapDebug, scheduleMapMarkerPositions, scheduleSelectedPopoverPosition]);

  const handleMapViewportChanging = useCallback(() => {
    publishMapDebug("viewport_changed", {}, "moveCount");
    if (!mapInteractingRef.current) {
      scheduleSelectedPopoverPosition();
    }
    scheduleMapMarkerPositions();
  }, [publishMapDebug, scheduleMapMarkerPositions, scheduleSelectedPopoverPosition]);

  useEffect(() => plannerStore.subscribe((state) => setPlannerSnapshot(state)), []);

  useEffect(() => {
    mapSearchRequestSeq.current += 1;
  }, [mapRequestContextKey]);

  useEffect(() => {
    if (previousMapContextKey.current === mapContextKey) {
      return;
    }
    const previousSessionId = previousMapContextKey.current.split(":")[0];
    previousMapContextKey.current = mapContextKey;
    if (previousSessionId === "__no_agent_session__") {
      return;
    }
    suppressNextPoiStoreSync.current = true;
    setPoiResults([]);
    setNearbyActive(false);
    nearbyBaseline.current = null;
    clearNearbyCircle();
    setPoiSearchState("idle");
    setPoiSearchError("");
    setPoiResolveState("idle");
    setPoiResolveMessage("");
    setPoiAddState("idle");
    setPoiAddMessage("");
    setPoiAddMessageTargetId(null);
    closePoiOverlays();
    plannerStore.setState({
      candidateMapPois: [],
      selectedMapPoi: null,
      poiSelectionStatuses: {}
    });
  }, [clearNearbyCircle, closePoiOverlays, mapContextKey]);

  useEffect(() => {
    if (!densityComparison) {
      return;
    }
    const activeCandidate = plannerSnapshot.pendingPoiCandidates.find(
      (candidate) => candidate.id === densityComparison.candidateRecordId
    );
    if (
      densityComparison &&
      previousDensityCandidateRecordId.current !==
        densityComparison.candidateRecordId
    ) {
      setSelectedPoiId(null);
      closePoiOverlays();
      plannerStore.setState({ selectedMapPoi: null });
      previousDensityCandidateRecordId.current =
        densityComparison.candidateRecordId;
    }
    if (densityComparison && !activeCandidate) {
      setPoiResults([]);
      setPoiSearchState("idle");
      return;
    }
    const recommendedPois = uniquePois(
      activeCandidate?.candidates ?? []
    );
    if (!recommendedPois.length) {
      if (densityComparison) {
        setPoiResults([]);
        setSelectedPoiId(null);
        setPoiSearchState("idle");
        plannerStore.setState({
          candidateMapPois: [],
          selectedMapPoi: null
        });
      }
      return;
    }
    setPoiResults(recommendedPois);
    setPoiSearchState("ready");
  }, [
    densityComparison,
    densityComparison?.candidateRecordId,
    closePoiOverlays,
    plannerSnapshot.pendingPoiCandidates
  ]);

  useEffect(() => {
    const currentRecordId = densityComparison?.candidateRecordId ?? null;
    if (
      previousDensityCandidateRecordId.current !== null &&
      currentRecordId === null
    ) {
      setPoiResults([]);
      setSelectedPoiId(null);
      closePoiOverlays();
      plannerStore.setState({
        candidateMapPois: [],
        selectedMapPoi: null
      });
    }
    previousDensityCandidateRecordId.current = currentRecordId;
  }, [closePoiOverlays, densityComparison?.candidateRecordId]);

  useEffect(() => {
    let cancelled = false;
    let activeMap: AMapInstance | null = null;
    const initAbortController = new AbortController();

    async function initializeAmap() {
      if (!mapNode.current) {
        return;
      }

      setMapState("loading");
      setMapError("");
      const releaseInitSlot = await acquireMapRuntimeSlot(initAbortController.signal);

      try {
        if (!releaseInitSlot || cancelled || initAbortController.signal.aborted) {
          return;
        }
        const config = await apiClient.getMapConfig();
        if (!config.enabled || !config.jsApiKey) {
          throw new Error("高德地图暂时不可用，请稍后重试。");
        }

        const userLocation = await resolveCurrentLocation({ useSlot: false, signal: initAbortController.signal });

        if (cancelled || initAbortController.signal.aborted) {
          return;
        }
        const amap = await loadAmap(config.jsApiKey, config.securityJsCode);
        if (cancelled || initAbortController.signal.aborted || !mapNode.current) {
          return;
        }

        mapInstance.current?.destroy?.();
        amapRuntime.current = amap;
        const initialCenter = userLocation ?? centerRef.current;
        activeMap = new amap.Map(mapNode.current, {
          animateEnable: false,
          buildingAnimation: false,
          ...(initialCenter ? { center: initialCenter } : {}),
          dragEnable: capabilitiesRef.current.navigate,
          zoomEnable: capabilitiesRef.current.navigate,
          scrollWheel: capabilitiesRef.current.navigate,
          doubleClickZoom: capabilitiesRef.current.navigate,
          features: ["bg", "road", "point"],
          jogEnable: false,
          mapStyle: "amap://styles/normal",
          pitch: 0,
          resizeEnable: true,
          rotation: 0,
          showIndoorMap: false,
          viewMode: "2D",
          zoom: mapPointsLengthRef.current > 1 ? 13 : 11
        });
        activeMap.on?.("dragstart", handleUserMapDrag);
        activeMap.on?.("dragend", handleMapViewportSettled);
        activeMap.on?.("movestart", handleMapMoveStart);
        activeMap.on?.("zoomstart", handleUserMapZoomStart);
        activeMap.on?.("click", handleBaseMapClick);
        activeMap.on?.("mapmove", handleMapViewportChanging);
        activeMap.on?.("zoomchange", handleMapViewportChanging);
        activeMap.on?.("moveend", handleMapViewportSettled);
        activeMap.on?.("zoomend", handleMapViewportSettled);
        activeMap.on?.("complete", handleMapViewportSettled);
        mapInstance.current = activeMap;
        activeMap.setStatus?.({
          dragEnable: capabilitiesRef.current.navigate,
          zoomEnable: capabilitiesRef.current.navigate,
          scrollWheel: capabilitiesRef.current.navigate,
          doubleClickZoom: capabilitiesRef.current.navigate
        });
        publishMapDebug("map_initialized", {
          initialized: true,
          mapState: "ready",
          mode: mapDebugStateRef.current.mode,
          capabilities: capabilitiesRef.current
        });
        allowInitialFitView.current = mapPointsLengthRef.current > 1;
        setMapState("ready");
      } catch (error) {
        setMapState("error");
        setMapError(error instanceof Error ? error.message : "高德地图加载失败");
      } finally {
        releaseInitSlot?.();
      }
    }
    initializeAmap();
    return () => {
      cancelled = true;
      initAbortController.abort();
      cancelPopoverFrame();
      cancelMarkerFrame();
      clearNearbyCircle();
      activeMap?.destroy?.();
    };
  }, [
    cancelMarkerFrame,
    cancelPopoverFrame,
    clearNearbyCircle,
    handleBaseMapClick,
    handleMapViewportChanging,
    handleMapViewportSettled,
    handleMapMoveStart,
    handleUserMapDrag,
    handleUserMapZoomStart,
    resolveCurrentLocation,
    mapLoadAttempt,
    publishMapDebug
  ]);

  useEffect(() => {
    if (!isActive || !mapInstance.current) {
      return;
    }
    const frame = window.requestAnimationFrame(() => mapInstance.current?.resize?.());
    return () => window.cancelAnimationFrame(frame);
  }, [isActive]);

  useEffect(() => {
    const node = mapNode.current;
    if (!node || typeof ResizeObserver === "undefined") return;
    let frame = 0;
    const observer = new ResizeObserver(() => {
      window.cancelAnimationFrame(frame);
      frame = window.requestAnimationFrame(() => {
        if (node.clientWidth > 0 && node.clientHeight > 0) {
          mapInstance.current?.resize?.();
          scheduleMapMarkerPositions({ force: true });
          scheduleSelectedPopoverPosition();
        }
      });
    });
    observer.observe(node);
    return () => {
      observer.disconnect();
      window.cancelAnimationFrame(frame);
    };
  }, [scheduleMapMarkerPositions, scheduleSelectedPopoverPosition]);

  useEffect(() => {
    if (mapState !== "ready" || !mapInstance.current) {
      return;
    }
    if (center) {
      moveMapTo(center);
    }
    closePoiOverlays();
    setNearbyActive(false);
    nearbyBaseline.current = null;
    clearNearbyCircle();
  }, [center, clearNearbyCircle, closePoiOverlays, mapState, moveMapTo]);

  useEffect(() => {
    if (mapState !== "ready" || !mapInstance.current || !amapRuntime.current) {
      return;
    }
    scheduleMapMarkerPositions();
    if (
      interactionMode === "itinerary_edit" &&
      allowInitialFitView.current &&
      mapPoints.length > 1 &&
      !didInitialFitView.current
    ) {
      fitMapToPoints(mapInstance.current, mapPoints);
      didInitialFitView.current = true;
    }
  }, [interactionMode, mapState, mapPoints, scheduleMapMarkerPositions, segments]);

  useEffect(() => {
    if (!comparisonViewportKey) {
      lastComparisonViewportKey.current = "";
      return;
    }
    if (
      mapState !== "ready" ||
      !mapInstance.current ||
      lastComparisonViewportKey.current === comparisonViewportKey
    ) {
      return;
    }
    lastComparisonViewportKey.current = comparisonViewportKey;
    fitMapToPoints(mapInstance.current, mapPoints);
    scheduleMapMarkerPositions();
    publishMapDebug("comparison_focus_fit");
  }, [comparisonViewportKey, mapPoints, mapState, publishMapDebug, scheduleMapMarkerPositions]);

  useEffect(() => {
    if (!densityComparison) {
      lastDensityComparisonFitKey.current = "";
      return;
    }
    if (
      mapState !== "ready" ||
      !mapInstance.current ||
      densityComparisonPoints.length < 2 ||
      densityComparisonFitKey === lastDensityComparisonFitKey.current
    ) {
      return;
    }
    fitMapToPoints(mapInstance.current, densityComparisonPoints);
    lastDensityComparisonFitKey.current = densityComparisonFitKey;
  }, [densityComparison, densityComparisonFitKey, densityComparisonPoints, mapState]);

  useEffect(() => {
    if (mapState !== "ready" || !mapInstance.current || !amapRuntime.current) {
      return;
    }
    syncSelectedRoutePolylines(amapRuntime.current, mapInstance.current);
  }, [mapState, selectedRouteOptionId, selectedRoutes, syncSelectedRoutePolylines]);

  useEffect(() => {
    if (mapState !== "ready" || !mapInstance.current || !amapRuntime.current) {
      return;
    }
    syncPreviewRoutePolyline(amapRuntime.current, mapInstance.current);
  }, [mapState, previewRoute?.id, syncPreviewRoutePolyline]);

  useEffect(() => {
    spatialBoundaryPolygon.current?.setMap?.(null);
    spatialBoundaryPolygon.current = null;
    const amap = amapRuntime.current;
    const map = mapInstance.current;
    const path = spatialBoundaryPreview?.polygonGcj02 ?? [];
    if (mapState !== "ready" || !map || !amap?.Polygon || !isValidClosedBoundaryPath(path)) {
      return;
    }
    const polygon = new amap.Polygon({
      path,
      strokeColor: "#2563eb",
      strokeWeight: 3,
      strokeOpacity: 0.9,
      fillColor: "#60a5fa",
      fillOpacity: 0.16,
      zIndex: 90,
      bubble: true
    });
    polygon.setMap?.(map);
    spatialBoundaryPolygon.current = polygon;
    fitMapToPoints(map, path);
    return () => {
      polygon.setMap?.(null);
      if (spatialBoundaryPolygon.current === polygon) {
        spatialBoundaryPolygon.current = null;
      }
    };
  }, [mapState, spatialBoundaryPreview]);

  useEffect(() => {
    if (mapState !== "ready") {
      markerElements.current.forEach((marker) => {
        marker.style.visibility = "hidden";
        marker.dataset.projected = "false";
      });
      return;
    }
    scheduleMapMarkerPositions();
  }, [mapState, poiResults, scheduleMapMarkerPositions, segments]);

  useEffect(() => {
    const storedPoi = plannerSnapshot.selectedMapPoi;
    if (!storedPoi) {
      if (selectedPoiId) {
        setSelectedPoiId(null);
        setPhotoViewerOpen(false);
        setCollapsedPopoverPoiId(null);
        setNearbyKeyword("");
        latestPopoverPosition.current = null;
        setPopoverPosition(null);
      }
      return;
    }
    if (selectedPoiId === storedPoi.id) {
      return;
    }
    const matchingPoi = poiResults.find((poi) => sameMapPoi(poi, storedPoi));
    if (!matchingPoi) {
      return;
    }
    setSelectedPoiId(matchingPoi.id);
  }, [plannerSnapshot.selectedMapPoi, poiResults, selectedPoiId]);

  useEffect(() => {
    if (mapState !== "ready" || !selectedSegmentId) {
      return;
    }
    if (plannerSnapshot.timelineSelectionRequestId !== lastTimelineSelectionRequestId.current) {
      return;
    }
    const segment = segments.find((item) => item.id === selectedSegmentId);
    if (!segment || !isMappableSegment(segment)) {
      return;
    }
    moveMapTo([segment.poi.longitude, segment.poi.latitude]);
  }, [mapState, moveMapTo, plannerSnapshot.timelineSelectionRequestId, selectedSegmentId, segments]);

  useEffect(() => {
    const requestId = plannerSnapshot.timelineSelectionRequestId;
    if (requestId === lastTimelineSelectionRequestId.current) {
      return;
    }
    lastTimelineSelectionRequestId.current = requestId;
    setDismissedTimelinePopoverSegmentId(null);
    selectedPoiIdRef.current = null;
    setSelectedPoiId(null);
    setCollapsedPopoverPoiId(null);
    setPhotoViewerOpen(false);
    clearStoredSelectedMapPoi();
    const segment = segments.find((item) => item.id === selectedSegmentId);
    if (mapState === "ready" && segment && isMappableSegment(segment)) {
      moveMapTo([segment.poi.longitude, segment.poi.latitude]);
    }
  }, [clearStoredSelectedMapPoi, mapState, moveMapTo, plannerSnapshot.timelineSelectionRequestId, selectedSegmentId, segments]);

  useEffect(() => {
    if (suppressNextPoiStoreSync.current) {
      suppressNextPoiStoreSync.current = false;
      selectedPoiRef.current = null;
      scheduleSelectedPopoverPosition();
      return;
    }
    plannerStore.setState({
      candidateMapPois: visiblePoiResults,
      ...(!showCandidatePoiResults ? { selectedMapPoi: null } : {}),
      ...(selectedPoi && !selectedPoiIsTimeline
        ? {
            selectedMapPoi: selectedPoi
          }
        : {})
    });
    selectedPoiRef.current = selectedPoiPopoverOpen ? selectedPoi : null;
    if (selectedPoiPopoverOpen) {
      scheduleSelectedPopoverPosition();
    }
  }, [scheduleSelectedPopoverPosition, selectedPoi, selectedPoiIsTimeline, selectedPoiPopoverOpen, showCandidatePoiResults, visiblePoiResults]);

  useEffect(() => {
    if (!selectedTimelinePoi || candidateSelectedPoi || !selectedPoiPopoverOpen || dismissedTimelinePopoverSegmentId === selectedSegmentId) {
      return;
    }
    selectedPoiRef.current = selectedTimelinePoi;
    setPhotoIndex(0);
    setPhotoViewerOpen(false);
    setPoiResolveState("idle");
    setPoiResolveMessage("");
    setPopoverPositionIfChanged(containerPositionForPoi(selectedTimelinePoi));
  }, [
    candidateSelectedPoi,
    containerPositionForPoi,
    dismissedTimelinePopoverSegmentId,
    selectedPoiPopoverOpen,
    selectedSegmentId,
    selectedTimelinePoi,
    setPopoverPositionIfChanged
  ]);

  useEffect(() => {
    function closeWhenClickingOutside(event: PointerEvent) {
      const target = event.target as HTMLElement | null;
      if (!target || !selectedPoiRef.current) {
        return;
      }
      if (target.closest(".poi-map-popover, .photo-viewer-panel")) {
        return;
      }
      if (target.closest(".amap-base")) {
        return;
      }
      dismissPoiOverlays();
    }

    document.addEventListener("pointerdown", closeWhenClickingOutside);
    return () => document.removeEventListener("pointerdown", closeWhenClickingOutside);
  }, [dismissPoiOverlays]);

  useEffect(() => {
    function clearSearchWhenPressingEscape(event: KeyboardEvent) {
      if (event.key !== "Escape") {
        return;
      }
      if (nearbyActive) {
        event.preventDefault();
        cancelNearbySearch();
        return;
      }
      if (poiResults.length || selectedPoiRef.current || poiSearchState === "error") {
        event.preventDefault();
        if (selectedPoiRef.current) {
          dismissPoiOverlays();
        }
        clearPoiSearchResults();
      }
    }

    document.addEventListener("keydown", clearSearchWhenPressingEscape);
    return () => document.removeEventListener("keydown", clearSearchWhenPressingEscape);
  }, [cancelNearbySearch, clearPoiSearchResults, dismissPoiOverlays, nearbyActive, poiResults.length, poiSearchState]);

  async function runBasicSearchRequest<T>(operation: () => Promise<T>): Promise<T> {
    const releaseSlot = await acquireBasicSearchSlot();
    try {
      return await operation();
    } finally {
      releaseSlot();
    }
  }

  function beginMapSearchRequest() {
    return {
      seq: ++mapSearchRequestSeq.current,
      contextKey: currentMapRequestContextKey.current
    };
  }

  function isCurrentMapSearchRequest(request: ReturnType<typeof beginMapSearchRequest>) {
    return mapSearchRequestSeq.current === request.seq && currentMapRequestContextKey.current === request.contextKey;
  }

  async function fetchPois(keyword: string, category: QuickCategory | "all") {
    if (!capabilitiesRef.current.search) {
      return;
    }
    const trimmedKeyword = keyword.trim();
    if (!trimmedKeyword) {
      setPoiSearchState("idle");
      setPoiSearchError("");
      return;
    }

    const request = beginMapSearchRequest();
    setPoiSearchState("loading");
    setPoiSearchError("");
    try {
      const response = await runBasicSearchRequest(() => apiClient.searchMapPois({ city, keyword: trimmedKeyword, category }));
      if (!isCurrentMapSearchRequest(request)) {
        return;
      }
      setPoiResults(response.pois);
      setNearbyActive(false);
      nearbyBaseline.current = null;
      clearNearbyCircle();
      setSelectedPoiId(null);
      setPhotoViewerOpen(false);
      setPoiResolveState("idle");
      setPoiResolveMessage("");
      setPoiSearchState("ready");
    } catch (error) {
      if (!isCurrentMapSearchRequest(request)) {
        return;
      }
      setPoiResults([]);
      setNearbyActive(false);
      nearbyBaseline.current = null;
      clearNearbyCircle();
      setSelectedPoiId(null);
      setPoiResolveState("idle");
      setPoiResolveMessage("");
      setPoiSearchState("error");
      setPoiSearchError(error instanceof Error ? error.message : "高德 POI 搜索失败");
    }
  }

  async function fetchNearbyPois(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!capabilitiesRef.current.search) {
      return;
    }
    const trimmedKeyword = nearbyKeyword.trim();
    if (!selectedPoi || !trimmedKeyword) {
      setPoiSearchError("附近搜索需要先选择地点并输入关键词");
      setPoiSearchState("error");
      return;
    }

    const request = beginMapSearchRequest();
    setPoiSearchState("loading");
    setPoiSearchError("");
    try {
      if (!nearbyActive) {
        nearbyBaseline.current = { pois: poiResults, selectedPoiId };
      }
      const originPoi = selectedPoi;
      const radiiToTry = NEARBY_RADIUS_OPTIONS.filter((radius) => radius >= nearbyRadius);
      const searchRadii = radiiToTry.length ? radiiToTry : [nearbyRadius];
      let matchedResponse: Awaited<ReturnType<typeof apiClient.searchNearbyMapPois>> | null = null;
      let attemptedRadius = searchRadii[0] ?? nearbyRadius;

      for (const radius of searchRadii) {
        attemptedRadius = radius;
        const response = await runBasicSearchRequest(() => apiClient.searchNearbyMapPois({
          city,
          longitude: originPoi.longitude,
          latitude: originPoi.latitude,
          keyword: trimmedKeyword,
          category: activeCategory,
          radius
        }));
        if (!isCurrentMapSearchRequest(request)) {
          return;
        }
        if (response.pois.length) {
          matchedResponse = response;
          break;
        }
      }

      if (!isCurrentMapSearchRequest(request)) {
        return;
      }
      setNearbyActive(true);
      setNearbyRadius(attemptedRadius);
      drawNearbyCircle(originPoi, attemptedRadius);
      setPhotoIndex(0);
      setPhotoViewerOpen(false);
      setPoiResolveState("idle");
      setPoiResolveMessage("");
      if (matchedResponse) {
        const nextSelectedPoi = densityComparison ? originPoi : (matchedResponse.pois[0] ?? null);
        if (nextSelectedPoi && !densityComparison) {
          plannerStore.setState({ selectedMapPoi: nextSelectedPoi });
        }
        setPoiResults(matchedResponse.pois);
        setSelectedPoiId(nextSelectedPoi?.id ?? null);
        setPoiSearchState("ready");
      } else {
        setPoiResults([]);
        setSelectedPoiId(null);
        setPoiSearchState("error");
        setPoiSearchError(`在 ${formatNearbyRadius(MAX_NEARBY_RADIUS)} 范围内未找到“${trimmedKeyword}”，请调整关键词或直接搜索其它地点。`);
      }
    } catch (error) {
      if (!isCurrentMapSearchRequest(request)) {
        return;
      }
      setPoiSearchState("error");
      setPoiSearchError(error instanceof Error ? error.message : "高德附近搜索失败");
    }
  }

  async function identifyPoiFromMapClick(event?: AMapMapEvent) {
    if (!capabilitiesRef.current.search) {
      return;
    }
    const clickedHint = clickedPoiHint(event);
    const clickPosition = clickedHint.location ?? lngLatToTuple(event?.lnglat);
    if (!clickPosition) {
      dismissPoiOverlays();
      setPoiSearchState("error");
      setPoiSearchError("无法识别该地图位置，请点击明确的地点名称或先搜索关键词。");
      return;
    }

    const keyword = clickedHint.name || "景点";
    const request = beginMapSearchRequest();
    setPoiSearchState("loading");
    setPoiSearchError("");
    try {
      const searchRadius = clickedHint.name ? 180 : 120;
      const response = await runBasicSearchRequest(() => apiClient.searchNearbyMapPois({
        city,
        longitude: clickPosition[0],
        latitude: clickPosition[1],
        keyword,
        category: "all",
        radius: searchRadius
      }));
      if (!isCurrentMapSearchRequest(request)) {
        return;
      }
      const nearestPoi = nearestReasonablePoi(response.pois, clickPosition, Boolean(clickedHint.name), keyword);
      if (!nearestPoi) {
        dismissPoiOverlays();
        setPoiSearchState("error");
        setPoiSearchError("未能准确识别该地点，请点击更明确的地名或使用搜索。");
        return;
      }

      setPoiResults(response.pois);
      setSelectedPoiId(nearestPoi.id);
      setPhotoIndex(0);
      setPhotoViewerOpen(false);
      setNearbyKeyword("");
      setPoiResolveState("idle");
      setPoiResolveMessage("");
      setPoiSearchState("ready");
      focusPoi(nearestPoi);
    } catch (error) {
      if (!isCurrentMapSearchRequest(request)) {
        return;
      }
      dismissPoiOverlays();
      setPoiSearchState("error");
      setPoiSearchError(error instanceof Error ? error.message : "高德地点反查失败");
    }
  }
  identifyPoiFromMapClickRef.current = (event?: AMapMapEvent) => {
    void identifyPoiFromMapClick(event);
  };

  function handleSearchSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!capabilitiesRef.current.search) {
      return;
    }
    fetchPois(searchKeyword, activeCategory);
  }

  function handleCategoryChange(category: QuickCategory) {
    if (!capabilitiesRef.current.search) {
      return;
    }
    setActiveCategory(category);
    setSearchKeyword(QUICK_CATEGORIES.find((option) => option.key === category)?.keyword ?? "");
    fetchPois(QUICK_CATEGORIES.find((option) => option.key === category)?.keyword ?? "", category);
  }

  function selectCurrentPoi() {
    if (!capabilitiesRef.current.mutateItinerary || !selectedPoi) {
      return;
    }
    plannerStore.setState({ selectedMapPoi: selectedPoi });
    setPoiAddState("saved");
    setPoiAddMessage(`当前 POI 已选择：${selectedPoi.name}`);
    setPoiAddMessageTargetId(selectedPoi.id);
  }

  async function addSelectedPoiToCurrentDay() {
    if (!capabilitiesRef.current.mutateItinerary) {
      return;
    }
    await addSelectedPoiToDay();
  }

  async function addSelectedPoiToDay(dayNumber?: number) {
    if (!capabilitiesRef.current.mutateItinerary) {
      return;
    }
    if (!selectedPoi) {
      return;
    }
    const snapshot = plannerStore.getSnapshot();
    const currentPlan = snapshot.itineraryPlan ?? (await createDraftPlanIfNeeded(city));
    if (!currentPlan) {
      setPoiAddState("error");
      setPoiAddMessage("行程草稿创建失败，无法加入当前 Day。");
      setPoiAddMessageTargetId(selectedPoi.id);
      return;
    }
    const targetDay =
      currentPlan.days.find((day) => day.dayNumber === (dayNumber ?? snapshot.selectedDayNumber)) ??
      currentPlan.days[0];
    if (!targetDay) {
      setPoiAddState("error");
      setPoiAddMessage("未找到可添加的 Day。");
      setPoiAddMessageTargetId(selectedPoi.id);
      return;
    }

    setPoiAddState("saving");
    setPoiAddMessage("");
    setPoiAddMessageTargetId(null);
    try {
      const preferenceText = snapshot.preferenceMemory?.memoryText ?? snapshot.preferenceCard?.summaryText ?? "";
      const pendingPoiCandidateId = activePendingCandidateIdForPoi(snapshot, selectedPoi);
      const baseVersionId = snapshot.activeVersionId;
      const result = await apiClient.patchItinerary(currentPlan.id, {
        sourceType: "manual",
        baseVersionId: baseVersionId ?? null,
        preferenceSummary: preferenceText,
        planningContext: {
          city,
          selectedDayNumber: targetDay.dayNumber,
          selectedMapPoi: selectedPoi,
          pendingPoiCandidates: snapshot.pendingPoiCandidates,
          candidateMapPois: snapshot.candidateMapPois,
          preferenceMemory: snapshot.preferenceMemory,
          currentPreferenceSummary: preferenceText,
          patchIntent: "map_add_selected_poi",
          pendingPoiCandidateId
        },
        operations: [
          {
            op: "add_segment",
            dayId: targetDay.id,
            title: selectedPoi.name,
            notes: selectedPoi.sourceNote,
            durationMinutes: 30,
            amapPoi: selectedPoi
          }
        ]
      });
      if (!isCurrentVersionedWrite(baseVersionId)) {
        setPoiAddState("idle");
        setPoiAddMessage("");
        setPoiAddMessageTargetId(null);
        return;
      }
      const updatedDay = result.itinerary.days.find((day) => day.id === targetDay.id) ?? result.itinerary.days[0];
      const nextSegment = updatedDay?.segments[updatedDay.segments.length - 1] ?? null;
      plannerStore.setState({
        agentSession: snapshot.agentSession
          ? {
              ...snapshot.agentSession,
              activeVersionId: result.version.id,
              itinerary: result.itinerary,
              pendingPoiCandidates: result.pendingPoiCandidates
            }
          : snapshot.agentSession,
        conversationTurns: snapshot.conversationTurns,
        itineraryPlan: result.itinerary,
        activeVersionId: result.version.id,
        pendingPoiCandidates: result.pendingPoiCandidates,
        selectedMapPoi: selectedPoi,
        poiSelectionStatuses: {
          ...snapshot.poiSelectionStatuses,
          [selectedPoi.id]: "addedToDay"
        },
        selectedDayNumber: updatedDay?.dayNumber ?? snapshot.selectedDayNumber,
        selectedSegmentId: nextSegment?.id ?? snapshot.selectedSegmentId,
        lastPatchError: ""
      });
      setPoiAddState("saved");
      setPoiAddMessage(`已加入 Day ${updatedDay?.dayNumber ?? targetDay.dayNumber}：${selectedPoi.name}`);
      setPoiAddMessageTargetId(selectedPoi.id);
    } catch (error) {
      const message = error instanceof Error ? error.message : "加入行程失败，请稍后重试";
      plannerStore.setState({ lastPatchError: message });
      setPoiAddState("error");
      setPoiAddMessage(message);
      setPoiAddMessageTargetId(selectedPoi.id);
    }
  }

  async function replaceSelectedTimelinePoi() {
    if (!capabilitiesRef.current.mutateItinerary) {
      return;
    }
    if (!selectedPoi || !selectedSegmentId || !plan) {
      setPoiAddState("error");
      setPoiAddMessage("请先在右侧时间轴选择要替换的地点。");
      setPoiAddMessageTargetId(null);
      return;
    }
    if (selected?.kind === "meal" && !isMealMapPoi(selectedPoi)) {
      setPoiAddState("error");
      setPoiAddMessage("当前选中的是餐饮段，只能替换为餐饮类地图地点。");
      setPoiAddMessageTargetId(null);
      return;
    }
    const requestId = poiReplacementRequestSeq.current + 1;
    poiReplacementRequestSeq.current = requestId;
    const snapshot = plannerStore.getSnapshot();
    const preferenceText = snapshot.preferenceMemory?.memoryText ?? snapshot.preferenceCard?.summaryText ?? "";
    const pendingPoiCandidateId = activePendingCandidateIdForPoi(snapshot, selectedPoi) ?? pendingCandidateIdForPoi(snapshot.pendingPoiCandidates, selectedPoi, selectedSegmentId);
    const replaceOperation = pendingPoiCandidateId
      ? {
          op: "replace_segment_poi_from_candidate" as const,
          segmentId: selectedSegmentId,
          candidateId: pendingPoiCandidateId,
          notes: selectedPoi.sourceNote || `已从高德候选替换为具体地点：${selectedPoi.name}`,
          amapPoi: selectedPoi
        }
      : {
          op: "replace_segment_poi" as const,
          segmentId: selectedSegmentId,
          notes: selectedPoi.sourceNote || `已替换为高德地图地点：${selectedPoi.name}`,
          amapPoi: selectedPoi
        };
    setPoiAddState("saving");
    setPoiAddMessage("");
    setPoiAddMessageTargetId(null);
    try {
      const baseVersionId = snapshot.activeVersionId;
      const result = await apiClient.patchItinerary(plan.id, {
        sourceType: "manual",
        baseVersionId: baseVersionId ?? null,
        preferenceSummary: preferenceText,
        planningContext: {
          city,
          selectedSegmentId,
          selectedMapPoi: selectedPoi,
          pendingPoiCandidates: snapshot.pendingPoiCandidates,
          candidateMapPois: snapshot.candidateMapPois,
          currentPreferenceSummary: preferenceText,
          patchIntent: "map_replace_selected_segment_poi",
          toolRefreshPolicy: { route: "touched_pairs_only" },
          pendingPoiCandidateId
        },
        operations: [replaceOperation]
      });
      applyMapPoiReplacementResult(result, baseVersionId, snapshot, selectedSegmentId, selectedPoi, undefined, requestId);
    } catch (error) {
      if (requestId !== poiReplacementRequestSeq.current) {
        return;
      }
      if (isVersionConflict(error)) {
        await recoverMapPoiReplacementConflict(replaceOperation, selectedPoi, selectedSegmentId, preferenceText, requestId);
        return;
      }
      const message = error instanceof Error ? error.message : "替换时间轴地点失败，请稍后重试";
      plannerStore.setState({ lastPatchError: message });
      setPoiAddState("error");
      setPoiAddMessage(message);
      setPoiAddMessageTargetId(selectedPoi.id);
    }
  }

  function applyMapPoiReplacementResult(
    result: Awaited<ReturnType<typeof apiClient.patchItinerary>>,
    baseVersionId: string | null,
    snapshot: ReturnType<typeof plannerStore.getSnapshot>,
    targetSegmentId: string,
    targetPoi: MapPoi,
    message = `已将当前时间轴地点替换为：${targetPoi.name}`,
    requestId?: number
  ) {
    if ((requestId !== undefined && requestId !== poiReplacementRequestSeq.current) || !isCurrentVersionedWrite(baseVersionId)) {
      return false;
    }
    plannerStore.setState({
      agentSession: snapshot.agentSession
        ? {
            ...snapshot.agentSession,
            activeVersionId: result.version.id,
            itinerary: result.itinerary,
            pendingPoiCandidates: result.pendingPoiCandidates
          }
        : snapshot.agentSession,
      itineraryPlan: result.itinerary,
      activeVersionId: result.version.id,
      selectedSegmentId: targetSegmentId,
      selectedDayNumber: dayNumberForMapSegment(result.itinerary, targetSegmentId) ?? snapshot.selectedDayNumber,
      selectedRouteOptionId: null,
      previewRouteOptionId: null,
      selectedMapPoi: targetPoi,
      pendingPoiCandidates: result.pendingPoiCandidates,
      routeWarnings: result.itinerary.routeWarnings ?? [],
      lastPatchError: ""
    });
    setPoiAddState("saved");
    setPoiAddMessage(message);
    setPoiAddMessageTargetId(targetPoi.id);
    return true;
  }

  async function recoverMapPoiReplacementConflict(
    operation: { op: "replace_segment_poi"; segmentId: string; notes: string; amapPoi: MapPoi } | { op: "replace_segment_poi_from_candidate"; segmentId: string; candidateId: string; notes: string; amapPoi: MapPoi },
    targetPoi: MapPoi,
    targetSegmentId: string,
    preferenceText: string,
    requestId: number
  ) {
    try {
      if (requestId !== poiReplacementRequestSeq.current) {
        return;
      }
      const session = await apiClient.getCurrentAgentSession();
      const latestPlan = session.itinerary;
      const refreshedSnapshot = plannerStore.getSnapshot();
      const selectedAfterRefresh = latestPlan && segmentExistsInMapPlan(latestPlan, targetSegmentId)
        ? targetSegmentId
        : latestPlan?.days[0]?.segments[0]?.id ?? null;
      const selectedDayAfterRefresh = latestPlan
        ? dayNumberForMapSegment(latestPlan, selectedAfterRefresh) ?? latestPlan.days[0]?.dayNumber ?? refreshedSnapshot.selectedDayNumber
        : refreshedSnapshot.selectedDayNumber;
      plannerStore.setState({
        agentSession: session,
        conversationTurns: session.turns,
        activeVersionId: session.activeVersionId ?? null,
        itineraryPlan: latestPlan,
        pendingPoiCandidates: session.pendingPoiCandidates,
        selectedSegmentId: selectedAfterRefresh,
        selectedDayNumber: selectedDayAfterRefresh,
        selectedRouteOptionId: null,
        previewRouteOptionId: null,
        routeWarnings: latestPlan?.routeWarnings ?? refreshedSnapshot.routeWarnings,
        lastPatchError: ""
      });
      if (!latestPlan || !segmentExistsInMapPlan(latestPlan, targetSegmentId) || !session.activeVersionId) {
        const message = "行程已更新，请基于最新时间轴重新选择要替换的地点。";
        plannerStore.setState({ lastPatchError: "" });
        setPoiAddState("error");
        setPoiAddMessage(message);
        setPoiAddMessageTargetId(targetPoi.id);
        return;
      }
      const latestSnapshot = plannerStore.getSnapshot();
      const latestBaseVersionId = latestSnapshot.activeVersionId;
      const retryResult = await apiClient.patchItinerary(latestPlan.id, {
        sourceType: "manual",
        baseVersionId: latestBaseVersionId ?? null,
        preferenceSummary: preferenceText,
        planningContext: {
          city,
          selectedSegmentId: targetSegmentId,
          selectedMapPoi: targetPoi,
          pendingPoiCandidates: latestSnapshot.pendingPoiCandidates,
          candidateMapPois: latestSnapshot.candidateMapPois,
          currentPreferenceSummary: preferenceText,
          patchIntent: "map_replace_selected_segment_poi_retry_after_version_refresh",
          toolRefreshPolicy: { route: "touched_pairs_only" },
          pendingPoiCandidateId: operation.op === "replace_segment_poi_from_candidate" ? operation.candidateId : null
        },
        operations: [operation]
      });
      applyMapPoiReplacementResult(
        retryResult,
        latestBaseVersionId,
        latestSnapshot,
        targetSegmentId,
        targetPoi,
        `已同步最新行程，并将当前时间轴地点替换为：${targetPoi.name}`,
        requestId
      );
    } catch (recoveryError) {
      if (requestId !== poiReplacementRequestSeq.current) {
        return;
      }
      const message = isVersionConflict(recoveryError)
        ? "行程已再次更新，请基于最新时间轴重新选择要替换的地点。"
        : recoveryError instanceof Error ? recoveryError.message : "同步最新行程失败，请稍后重试。";
      plannerStore.setState({ lastPatchError: message });
      setPoiAddState("error");
      setPoiAddMessage(message);
      setPoiAddMessageTargetId(targetPoi.id);
    }
  }

  async function ignoreSelectedPoi() {
    if (!capabilitiesRef.current.mutateItinerary || !selectedPoi) {
      return;
    }
    const snapshot = plannerStore.getSnapshot();
    const candidateId = activePendingCandidateIdForPoi(snapshot, selectedPoi);
    if (!candidateId) {
      plannerStore.setState({
        poiSelectionStatuses: {
          ...snapshot.poiSelectionStatuses,
          [selectedPoi.id]: "ignored"
        }
      });
      setPoiAddState("saved");
      setPoiAddMessage(`已忽略：${selectedPoi.name}`);
      setPoiAddMessageTargetId(selectedPoi.id);
      return;
    }
    if (!snapshot.agentSession) {
      setPoiAddState("error");
      setPoiAddMessage("未找到可持久化忽略的 pending POI。");
      setPoiAddMessageTargetId(selectedPoi.id);
      return;
    }
    setPoiAddState("saving");
    setPoiAddMessage("");
    setPoiAddMessageTargetId(null);
    try {
      const session = await apiClient.rejectPendingPoiCandidate(snapshot.agentSession.sessionId, candidateId);
      plannerStore.setState({
        agentSession: session,
        conversationTurns: session.turns,
        activeVersionId: session.activeVersionId ?? snapshot.activeVersionId,
        itineraryPlan: session.itinerary ?? snapshot.itineraryPlan,
        pendingPoiCandidates: session.pendingPoiCandidates,
        poiSelectionStatuses: {
          ...snapshot.poiSelectionStatuses,
          [selectedPoi.id]: "ignored"
        }
      });
      setPoiAddState("saved");
      setPoiAddMessage(`已忽略：${selectedPoi.name}`);
      setPoiAddMessageTargetId(selectedPoi.id);
    } catch (error) {
      setPoiAddState("error");
      setPoiAddMessage(error instanceof Error ? error.message : "忽略 POI 失败，请稍后重试");
      setPoiAddMessageTargetId(selectedPoi.id);
    }
  }

  function openSelectedPoiSource() {
    if (!selectedPoi) {
      return;
    }
    const sourceUrl = selectedPoi.sourceUrl || selectedPoi.photos[0]?.url;
    if (sourceUrl) {
      window.open(sourceUrl, "_blank", "noopener,noreferrer");
      return;
    }
    setPoiAddState("saved");
    setPoiAddMessage(selectedPoi.sourceNote || "暂无可信实时来源，请以官方渠道确认为准。");
    setPoiAddMessageTargetId(selectedPoi.id);
  }

  async function createDraftPlanIfNeeded(cityName: string) {
    const snapshot = plannerStore.getSnapshot();
    if (snapshot.itineraryPlan) {
      return snapshot.itineraryPlan;
    }
    const session = snapshot.agentSession
      ? await apiClient.getAgentSession(snapshot.agentSession.sessionId)
      : await apiClient.createAgentSession({
          city: cityName,
          title: `${cityName} AI 行程`,
          preferenceCardId: snapshot.preferenceCard?.id
        });
    plannerStore.setState({
      agentSession: session,
      conversationTurns: session.turns,
      activeVersionId: session.activeVersionId ?? null,
      itineraryPlan: session.itinerary,
      pendingPoiCandidates: session.pendingPoiCandidates
    });
    return session.itinerary;
  }

  async function deleteSelectedItinerarySegment() {
    if (!capabilitiesRef.current.mutateItinerary) {
      return;
    }
    if (!selected || !plan) {
      return;
    }
    if (!window.confirm(`确认从行程中删除 ${selected.poi.name} 吗？相关路线会重新规划或标记为待重新规划。`)) {
      return;
    }
    const snapshot = plannerStore.getSnapshot();
    setPoiAddState("saving");
    setPoiAddMessage("");
    setPoiAddMessageTargetId(null);
    try {
      const baseVersionId = snapshot.activeVersionId;
      const preferenceText = snapshot.preferenceMemory?.memoryText ?? snapshot.preferenceCard?.summaryText ?? "";
      const result = await apiClient.patchItinerary(plan.id, {
        sourceType: "manual",
        baseVersionId: baseVersionId ?? null,
        preferenceSummary: preferenceText,
        planningContext: {
          city,
          selectedSegmentId: selected.id,
          selectedDayNumber: snapshot.selectedDayNumber,
          pendingPoiCandidates: snapshot.pendingPoiCandidates,
          candidateMapPois: snapshot.candidateMapPois,
          currentPreferenceSummary: preferenceText,
          preferenceMemory: snapshot.preferenceMemory,
          patchIntent: "map_remove_segment",
          toolRefreshPolicy: { route: "touched_pairs_only" }
        },
        operations: [{ op: "remove_segment", segmentId: selected.id }]
      });
      if (!isCurrentVersionedWrite(baseVersionId)) {
        setPoiAddState("idle");
        setPoiAddMessage("");
        setPoiAddMessageTargetId(null);
        return;
      }
      const nextSelected = nextSegmentAfterMapRemoval(plan, result.itinerary, selected.id);
      plannerStore.setState({
        agentSession: snapshot.agentSession
          ? {
              ...snapshot.agentSession,
              activeVersionId: result.version.id,
              itinerary: result.itinerary,
              pendingPoiCandidates: result.pendingPoiCandidates
            }
          : snapshot.agentSession,
        itineraryPlan: result.itinerary,
        activeVersionId: result.version.id,
        pendingPoiCandidates: result.pendingPoiCandidates,
        candidateMapPois: [],
        selectedMapPoi: null,
        selectedSegmentId: nextSelected?.id ?? null,
        selectedDayNumber: dayNumberForMapSegment(result.itinerary, nextSelected?.id ?? null) ?? snapshot.selectedDayNumber,
        selectedRouteOptionId: null,
        previewRouteOptionId: null,
        routeWarnings: result.itinerary.routeWarnings ?? [],
        lastPatchError: ""
      });
      setPoiResults([]);
      selectedPoiIdRef.current = null;
      setSelectedPoiId(null);
      setNearbyActive(false);
      setPopoverPosition(null);
      setPhotoViewerOpen(false);
      setPoiAddState("saved");
      setPoiAddMessage(`已删除：${selected.poi.name}`);
      setPoiAddMessageTargetId(null);
    } catch (error) {
      const message = error instanceof Error ? error.message : "删除景点失败，请稍后重试";
      plannerStore.setState({ lastPatchError: message });
      setPoiAddState("error");
      setPoiAddMessage(message);
      setPoiAddMessageTargetId(null);
    }
  }

  async function resolveSelectedPoi() {
    if (!capabilitiesRef.current.mutateItinerary || !selectedPoi) {
      return;
    }
    setPoiResolveState("loading");
    setPoiResolveMessage("");
    try {
      const snapshot = plannerStore.getSnapshot();
      let sessionId = snapshot.agentSession?.sessionId;
      if (!sessionId) {
        await createDraftPlanIfNeeded(city);
        sessionId = plannerStore.getSnapshot().agentSession?.sessionId;
      }
      if (!sessionId) {
        throw new Error("未找到可持久化 POI 候选的 Agent 会话。");
      }
      const response = await apiClient.resolveMapPois({
        sessionId,
        city,
        queries: [{ name: selectedPoi.name, category: selectedPoi.category }]
      });
      if (response.resolved.length) {
        const accepted = response.resolved[0].poi;
        setPoiResolveState("accepted");
        setPoiResolveMessage(`POI 已通过高德验证：${accepted.name}`);
        plannerStore.setState({ selectedMapPoi: accepted });
        return;
      }
      if (response.pending.length) {
        const pending = response.pending[0];
        setPoiResolveState("pending");
        setPoiResolveMessage(`需要确认 POI：${pending.reason}`);
        plannerStore.setState({
          pendingPoiCandidates: response.pending.map((item) => ({
            id: item.candidateRecordId,
            query: item.query,
            city,
            category: selectedPoi.category,
            status: "pending",
            candidates: item.candidates,
            createdAt: new Date().toISOString()
          }))
        });
        return;
      }
      setPoiResolveState("pending");
      setPoiResolveMessage("需要确认 POI：no_results");
    } catch (error) {
      setPoiResolveState("error");
      setPoiResolveMessage(error instanceof Error ? error.message : "高德 POI 验证失败");
    }
  }

  function zoomIn() {
    runMapCommand("地图放大不可用", (map) => map.zoomIn?.());
  }

  function zoomOut() {
    runMapCommand("地图缩小不可用", (map) => map.zoomOut?.());
  }

  function resetView() {
    runMapCommand("重置视角不可用", (map) => {
      if (mapPoints.length > 1) {
        fitMapToPoints(map, mapPoints);
        return;
      }
      if (typeof map.panTo === "function") {
        map.panTo(center);
      } else if (typeof map.setCenter === "function") {
        map.setCenter(center);
      } else {
        map.setZoomAndCenter?.(map.getZoom?.() ?? 11, center);
      }
    });
  }

  function runMapCommand(message: string, command: (map: AMapInstance) => void) {
    if (mapState !== "ready" || !mapInstance.current) {
      setMapError(message);
      setMapState("error");
      return;
    }
    try {
      command(mapInstance.current);
    } catch (error) {
      console.warn("AMap map command failed", error);
      setMapError(message);
    }
  }

  function nextPhoto() {
    if (!selectedPoi?.photos.length) {
      return;
    }
    setPhotoIndex((current) => (current + 1) % selectedPoi.photos.length);
  }

  function previousPhoto() {
    if (!selectedPoi?.photos.length) {
      return;
    }
    setPhotoIndex((current) => (current + selectedPoi.photos.length - 1) % selectedPoi.photos.length);
  }

  function scrollPoiStripWithWheel(event: WheelEvent<HTMLDivElement>) {
    const strip = event.currentTarget;
    if (strip.scrollWidth <= strip.clientWidth || Math.abs(event.deltaX) >= Math.abs(event.deltaY)) {
      return;
    }
    event.preventDefault();
    strip.scrollLeft += event.deltaY;
  }

  const mapControlsDisabled = mapState !== "ready";

  return (
    <section className="planner-map" aria-label="map planning area" onClickCapture={(event) => {
      const opener = (event.target as HTMLElement).closest<HTMLElement>(".map-dom-marker, .poi-strip-card, .poi-marker, .poi-photo-card");
      if (opener) {
        poiKeyboardOpener.current = opener;
        if (event.detail === 0) window.requestAnimationFrame(() => poiPopoverRef.current?.querySelector<HTMLElement>("input, button")?.focus());
      }
    }}>
      {densityComparison ? (
        <div className="map-toolbar map-density-toolbar" aria-label="候选位置对比模式">
          <strong>
            Day {densityComparison.dayNumber} · {densityComparison.timeWindow} ·{" "}
            {densityComparison.displayNeed}
          </strong>
          <span>地图仅用于位置比较；请在对话栏选择最终地点。</span>
        </div>
      ) : readOnlyMap ? (
        <div className="map-toolbar map-density-toolbar" aria-label="方案地图只读预览">
          <strong>{interactionMode === "plan_comparison_preview" ? "多方案对比预览" : "方案总览预览"}</strong>
          <span>可拖拽、缩放、选择标记并查看详情；采用方案后才开放搜索与行程写入。</span>
        </div>
      ) : (
      <form className="map-toolbar" aria-label="Map filters and search" onSubmit={handleSearchSubmit}>
        <label>
          <span className="sr-only">地图搜索</span>
          <input
            placeholder="搜索景点 / 餐厅 / 体验"
            value={searchKeyword}
            onChange={(event) => setSearchKeyword(event.target.value)}
          />
        </label>
        <button className="layer-button" type="submit">搜索</button>
        <div className="filter-tabs" aria-label="POI filters">
          {QUICK_CATEGORIES.map((option) => (
            <button
              className={activeCategory === option.key ? "active" : ""}
              key={option.key}
              onClick={() => handleCategoryChange(option.key)}
              type="button"
            >
              {option.label}
            </button>
          ))}
        </div>
      </form>
      )}
      <div
        className={`map-stage${mapInteracting ? " map-interacting" : ""}`}
        data-map-mode={interactionMode}
        data-map-can-navigate={String(capabilities.navigate)}
        data-map-can-search={String(capabilities.search)}
        data-map-can-mutate={String(capabilities.mutateItinerary)}
        onWheelCapture={handleMapWheel}
        onPointerDownCapture={(event) => {
          const target = event.target as HTMLElement;
          const pointerTarget = [target.tagName.toLowerCase(), target.id, ...Array.from(target.classList)]
            .filter(Boolean)
            .join(".")
            .slice(0, 180);
          publishMapDebug("pointerdown", { pointerTarget });
        }}
      >
        <div ref={mapNode} className={`amap-base ${mapState === "ready" ? "ready" : ""}`} />
        {mapState === "loading" ? <div className="map-status-overlay">正在加载高德地图...</div> : null}
        {mapState === "error" ? (
          <div className="map-status-overlay error" role="alert">
            <strong>地图操作失败</strong>
            <span>{mapError}</span>
            <button onClick={() => setMapLoadAttempt((attempt) => attempt + 1)} type="button">重试地图</button>
            <span>地图暂不可用时，你仍可在 Agent 与行程面板继续编辑。</span>
          </div>
        ) : null}
        {mapState === "ready" ? (
          <div className="map-dom-marker-layer" aria-label="地图 POI 标记">
            {!densityComparison && segments.map((segment, index) => {
              if (!isMappableSegment(segment)) {
                return null;
              }
              const markerKey = `segment:${segment.id}`;
              const selectedMarker = segment.id === selectedSegmentId;
              return (
                <button
                  aria-label={`选择 ${segment.poi.name}`}
                  className={`map-dom-marker itinerary${selectedMarker ? " selected" : ""}`}
                  key={`segment-marker-${segment.id}`}
                  style={{ opacity: segmentOpacityOverrides[segment.id] ?? 1 }}
                  onClick={(event) => {
                    event.stopPropagation();
                    selectTimelineSegmentOnMap(segment.id);
                  }}
                  ref={(element) => {
                    if (element) {
                      markerElements.current.set(markerKey, element);
                    } else {
                      markerElements.current.delete(markerKey);
                    }
                  }}
                  type="button"
                >
                  <span
                    className={`amap-poi-pin${selectedMarker ? " selected" : ""}`}
                    aria-hidden="true"
                    style={{ "--plan-marker-color": segmentColorOverrides[segment.id] } as CSSProperties}
                  />
                  <span
                    aria-hidden="true"
                    className={`amap-poi-label${selectedMarker ? " selected" : ""}`}
                    data-label={`${index + 1}. ${segment.startTime} ${segment.poi.name}`}
                  />
                </button>
              );
            })}
            {densityComparisonAnchors.map((anchor, index) => {
              const markerKey = `density-anchor:${anchor.id}`;
              return (
                <button
                  aria-label={`Day ${densityComparison?.dayNumber ?? ""} 已安排地点 ${anchor.name}`}
                  className={`map-dom-marker density-anchor${anchor.id === selectedPoiId ? " selected" : ""}`}
                  key={markerKey}
                  onClick={(event) => {
                    event.stopPropagation();
                    handlePoiSelect(anchor);
                  }}
                  ref={(element) => {
                    if (element) {
                      markerElements.current.set(markerKey, element);
                    } else {
                      markerElements.current.delete(markerKey);
                    }
                  }}
                  type="button"
                >
                  <span className="amap-poi-pin" aria-hidden="true" />
                  <span
                    aria-hidden="true"
                    className="amap-poi-label"
                    data-label={`${index + 1}. ${anchor.startTime || anchor.timeWindow || ""} ${anchor.name}`}
                  />
                </button>
              );
            })}
            {visiblePoiResults.map((poi) => {
              const markerKey = `poi:${poi.id}`;
              const selectedMarker = poi.id === selectedPoiId;
              return (
                <button
                  aria-label={`选择 ${poi.name}`}
                  className={`map-dom-marker candidate${selectedMarker ? " selected" : ""}`}
                  key={`poi-marker-${poi.id}`}
                  onClick={(event) => {
                    event.stopPropagation();
                    handlePoiSelect(poi);
                  }}
                  ref={(element) => {
                    if (element) {
                      markerElements.current.set(markerKey, element);
                    } else {
                      markerElements.current.delete(markerKey);
                    }
                  }}
                  type="button"
                >
                  <span className={`amap-poi-pin${selectedMarker ? " selected" : ""}`} aria-hidden="true" />
                  <span
                    aria-hidden="true"
                    className={`amap-poi-label${selectedMarker ? " selected" : ""}`}
                    data-label={
                      densityComparison
                        ? `候选 · ${densityComparison.timeWindow} · ${poi.name}`
                        : poi.name
                    }
                  />
                </button>
              );
            })}
          </div>
        ) : null}
        <div className="map-summary-card">
          <span>全程距离</span>
          <strong>{totalDistance ? `${(totalDistance / 1000).toFixed(1)} km` : "待生成"}</strong>
          <span>预计耗时</span>
          <strong>{totalDuration ? `${totalDuration} 分钟` : "待规划"}</strong>
          <span className="smooth-dot">高德地图路径参考</span>
        </div>
        {!densityComparison && mapState !== "ready" && segments.map((segment, index) => (
          isMappablePoi(segment.poi) ? (
            <div
              className={`poi-map-card poi-pos-${index % 8} ${segment.id === selected?.id ? "selected" : ""}`}
              key={segment.id}
            >
              <button
                className="poi-marker"
                onClick={() => {
                  selectTimelineSegmentOnMap(segment.id);
                }}
                type="button"
                aria-label={`选择 ${segment.poi.name}`}
              >
                {index + 1}
              </button>
              <button
                className="poi-photo-card"
                onClick={() => {
                  selectTimelineSegmentOnMap(segment.id);
                }}
                type="button"
              >
                <span className={`poi-photo poi-photo-${index % 5}`} aria-hidden="true" />
                <strong>{segment.poi.name} · {segment.poi.category}</strong>
                <small>{segment.startTime} - {segment.endTime}</small>
              </button>
            </div>
          ) : null
        ))}
        {selectedPoi && selectedPoiPopoverOpen ? (
          <div className="poi-map-popover" ref={poiPopoverRef} onKeyDown={(event) => {
            if (event.key === "Escape") {
              event.preventDefault(); event.stopPropagation();
              dismissPoiOverlays();
              const opener = poiKeyboardOpener.current;
              if (opener?.isConnected && opener.getClientRects().length) opener.focus();
              poiKeyboardOpener.current = null;
            }
          }} style={popoverPosition ? { left: popoverPosition.left, top: popoverPosition.top } : undefined}>
            {capabilities.search ? <form className="nearby-search" onSubmit={fetchNearbyPois}>
              <input
                aria-label="附近搜索"
                placeholder="搜附近地点"
                value={nearbyKeyword}
                onChange={(event) => setNearbyKeyword(event.target.value)}
                onFocus={showNearbyRadiusCircle}
              />
              <div className="nearby-search-controls">
                <select
                  aria-label="附近搜索范围"
                  value={nearbyRadius}
                  onChange={(event) => {
                    const radius = Number(event.target.value);
                    setNearbyRadius(radius);
                    if (selectedPoi) {
                      drawNearbyCircle(selectedPoi, radius);
                    }
                  }}
                >
                  {NEARBY_RADIUS_OPTIONS.map((radius) => (
                    <option key={radius} value={radius}>{formatNearbyRadius(radius)}</option>
                  ))}
                </select>
                <button type="submit">查找</button>
                <button disabled={!nearbyActive} onClick={cancelNearbySearch} type="button">取消</button>
              </div>
            </form> : null}
            <button className="poi-map-thumbnail" onClick={() => setPhotoViewerOpen(true)} type="button">
              {selectedPoi.photos[0] ? (
                <img alt={selectedPoi.photos[0].title || selectedPoi.name} src={selectedPoi.photos[0].url} />
              ) : (
                <span>当前地点暂无照片</span>
              )}
              <strong>{selectedPoi.name}</strong>
            </button>
            {selectedPoiIsTimeline ? (
              <>
                <div className="poi-popover-actions">
                  <button onClick={openSelectedPoiSource} type="button">查看来源</button>
                </div>
                {readOnlyMap ? (
                  <div className="map-density-comparison-note">
                    <strong>只读方案预览</strong>
                    <span>地点仅用于比较，不会触发搜索、添加、替换或路线写入。</span>
                  </div>
                ) : null}
              </>
            ) : (
              densityComparison ? (
                <div className="map-density-comparison-note">
                  <strong>
                    Day {densityComparison.dayNumber} · {densityComparison.timeWindow} ·{" "}
                    {densityComparison.displayNeed}
                  </strong>
                  {selectedDensityCandidateChoice ? (
                    <>
                      <span>仅供路线位置比较；确认仍通过该候选已有的对话选择执行。</span>
                      <button
                        onClick={() => void onConfirmDensityCandidate?.(selectedDensityCandidateChoice)}
                        type="button"
                      >
                        确认并补入时间轴
                      </button>
                    </>
                  ) : (
                    <span>该地点尚未形成持久化候选，请先在对话栏搜索并取得候选。</span>
                  )}
                  <small>地图不会直接添加、替换地点或写入路线。</small>
                </div>
              ) : readOnlyMap ? (
                <div className="map-density-comparison-note">
                  <strong>只读方案预览</strong>
                  <span>地点仅用于比较，不会触发搜索、添加、替换或路线写入。</span>
                </div>
              ) : (
              <>
                <button className="poi-resolve-button" disabled={poiResolveState === "loading"} onClick={resolveSelectedPoi} type="button">
                  {poiResolveState === "loading" ? "验证中" : "验证 POI"}
                </button>
                <div className="poi-popover-actions">
                  <button onClick={selectCurrentPoi} type="button">选为当前 POI</button>
                  <button disabled={poiAddState === "saving"} onClick={addSelectedPoiToCurrentDay} type="button">
                    {poiAddState === "saving" ? "加入中" : "加入当前 Day"}
                  </button>
                  {selectedSegmentId ? (
                    <button disabled={poiAddState === "saving"} onClick={replaceSelectedTimelinePoi} type="button">
                      {poiAddState === "saving" ? "替换中" : "替换当前地点"}
                    </button>
                  ) : null}
                  {(plannerSnapshot.itineraryPlan?.days ?? plan?.days ?? [{ dayNumber: 1 }]).slice(0, 4).map((day) => (
                    <button
                      disabled={poiAddState === "saving"}
                      key={`add-${selectedPoi.id}-${day.dayNumber}`}
                      onClick={() => addSelectedPoiToDay(day.dayNumber)}
                      type="button"
                    >
                      加入 Day{day.dayNumber}
                    </button>
                  ))}
                  <button onClick={ignoreSelectedPoi} type="button">忽略该 POI</button>
                  <button onClick={openSelectedPoiSource} type="button">查看来源</button>
                </div>
              </>
              )
            )}
            {popoverPoiAddMessage ? (
              <p className={poiAddState === "error" ? "map-inline-error" : "map-inline-status"}>{popoverPoiAddMessage}</p>
            ) : null}
          </div>
        ) : null}
        <div className="map-control-stack" aria-label="Map controls">
          <button disabled={mapControlsDisabled} onClick={zoomIn} type="button">+</button>
          <button disabled={mapControlsDisabled} onClick={zoomOut} type="button">−</button>
          <button disabled={mapControlsDisabled} onClick={resetView} type="button">↻</button>
          <button
            className="active"
            disabled
            title="当前固定 2D 地图，3D 模式待稳定接入"
            type="button"
          >
            2D
          </button>
        </div>
        <span className="scale-chip">高德地图</span>
      </div>
      <div className="map-details">
        <div className="map-route-copy">
          <h2>{plan?.title ?? `${city}行程地图`}</h2>
          {comparisonLegend.length ? (
            <div className="map-comparison-legend" aria-label="方案地图图例">
              {comparisonLegend.map((item) => (
                <button
                  aria-pressed={item.focused}
                  className={item.focused ? "focused" : ""}
                  key={item.id}
                  onClick={() => onFocusComparisonPlan?.(item.id)}
                  type="button"
                >
                  <i aria-hidden="true" style={{ backgroundColor: item.color }} />{item.label}
                </button>
              ))}
            </div>
          ) : null}
          {selectedPoi ? (
            <p>当前选中：{selectedPoi.name}</p>
          ) : selected ? (
            <p>当前选中：{selected.poi.name}</p>
          ) : (
            <p>生成行程后展示 POI、路线和距离。</p>
          )}
          {routeSummaries.length ? (
            <div className="route-summary">
              <p>当天路线 {(totalDistance / 1000).toFixed(1)} km · {totalDuration} 分钟</p>
              <div className="map-route-list" aria-label="当天地图路线列表">
                {routeSummaries.map((item) => (
                  <article className="map-route-item" key={item.id} style={{ borderLeftColor: routeColorOverrides[item.id] ?? routeLegColor(item, routeColorMap), opacity: routeOpacityOverrides[item.id] ?? 1 }}>
                    <strong>{routeEndpointName(item, routeSegmentsById, "from")} → {routeEndpointName(item, routeSegmentsById, "to")}</strong>
                    <span>
                      {(item.distanceMeters / 1000).toFixed(1)} km · {item.durationMinutes} 分钟 · {item.label || transportLabel(item.mode || item.transportMode)} · 来源：{routeSourceLabel(item.source || item.provider)}
                    </span>
                  </article>
                ))}
              </div>
              {previewRoute ? <p>正在预览路线，地图高亮显示当前候选。</p> : null}
              {selected && capabilities.mutateItinerary ? (
                <button className="map-delete-segment-button" onClick={deleteSelectedItinerarySegment} type="button">
                  删除当前景点
                </button>
              ) : null}
            </div>
          ) : (
            <p>路线待地点确认后生成</p>
          )}
          {poiSearchState === "error" ? <p className="map-inline-error">{poiSearchError}</p> : null}
          {poiSearchState === "loading" ? <p>正在查询高德 POI...</p> : null}
          {poiResolveMessage ? (
            <p className={poiResolveState === "error" ? "map-inline-error" : "map-inline-status"}>{poiResolveMessage}</p>
          ) : null}
          {visiblePoiAddMessage && !popoverPoiAddMessage ? (
            <p className={poiAddState === "error" ? "map-inline-error" : "map-inline-status"}>{visiblePoiAddMessage}</p>
          ) : null}
        </div>
        {showCandidatePoiResults ? <><div className="poi-carousel-header">
          <h3>候选 POI</h3>
        </div>
        <div className="poi-strip" aria-label="Recommended POI cards" onWheel={scrollPoiStripWithWheel}>
          {visiblePoiResults.length ? (
            visiblePoiResults.map((poi) => (
              <button
                className={`poi-strip-card ${poi.id === selectedPoiId ? "selected" : ""}`}
                key={poi.id}
                onClick={() => handlePoiSelect(poi)}
                type="button"
              >
                <span className={`poi-strip-media ${poi.photos[0] ? "" : "no-photo"}`} aria-hidden={poi.photos[0] ? undefined : "true"}>
                  {poi.photos[0] ? (
                    <img alt={poi.photos[0].title || poi.name} className="poi-strip-image" src={poi.photos[0].url} />
                  ) : null}
                </span>
                <strong>{poi.name}</strong>
                <small>{poi.type || "高德 POI"} · {poi.district}</small>
                <small>{poi.address || "来源：高德地图"}</small>
              </button>
            ))
          ) : (
            <p className="empty-map-hint">输入关键词或点击快捷分类后展示高德 POI 结果。</p>
          )}
        </div></> : null}
      </div>
      {selectedPoi && photoViewerOpen ? (
        <FocusDialog
          className="photo-viewer"
          label={`${selectedPoi.name} 照片查看器`}
          onClose={() => setPhotoViewerOpen(false)}
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) {
              setPhotoViewerOpen(false);
            }
          }}
        >
          <div className="photo-viewer-panel">
            <header>
              <div>
                <h2>{selectedPoi.name}</h2>
                <p className="photo-provider-note">来源：高德地图</p>
              </div>
              <button onClick={() => setPhotoViewerOpen(false)} type="button">关闭</button>
            </header>
            {selectedPhoto ? (
              <div className="photo-viewer-body">
                <button onClick={previousPhoto} type="button">‹</button>
                <img alt={selectedPhoto.title || selectedPoi.name} src={selectedPhoto.url} />
                <button onClick={nextPhoto} type="button">›</button>
              </div>
            ) : (
              <p className="photo-empty">当前地点暂无照片</p>
            )}
            <footer>
              {selectedPhoto?.url ? (
                <a href={selectedPhoto.url} target="_blank" rel="noreferrer">
                  来源网址：{selectedPhoto.url}
                </a>
              ) : (
                <span>高德未返回照片 URL</span>
              )}
              <span className="photo-meta-text">{selectedPoi.address || selectedPoi.district || selectedPoi.type}</span>
            </footer>
          </div>
        </FocusDialog>
      ) : null}
    </section>
  );
}

function transportLabel(mode: string) {
  const labels: Record<string, string> = {
    public_transit: "公共交通",
    transit: "公交/地铁",
    self_drive: "自驾",
    driving: "驾车",
    walking: "步行",
    walk: "步行",
    bicycling: "骑行",
    taxi: "打车"
  };
  return labels[mode] ?? mode;
}

function formatNearbyRadius(radius: number) {
  return radius >= 1000 ? `${radius / 1000}km` : `${radius}m`;
}

function routeSourceLabel(source?: string | null) {
  if (!source) {
    return "地图服务";
  }
  if (/amap|高德/i.test(source)) {
    return "高德地图";
  }
  if (/mock/i.test(source)) {
    return "地图服务（真实路线待接入）";
  }
  return "地图服务";
}

function fitMapToPoints(map: AMapInstance, points: [number, number][]) {
  if (!points.length) {
    return;
  }
  try {
    const bounds = points.reduce(
      (result, [longitude, latitude]) => ({
        minLongitude: Math.min(result.minLongitude, longitude),
        maxLongitude: Math.max(result.maxLongitude, longitude),
        minLatitude: Math.min(result.minLatitude, latitude),
        maxLatitude: Math.max(result.maxLatitude, latitude)
      }),
      {
        minLongitude: points[0][0],
        maxLongitude: points[0][0],
        minLatitude: points[0][1],
        maxLatitude: points[0][1]
      }
    );
    const center: [number, number] = [
      (bounds.minLongitude + bounds.maxLongitude) / 2,
      (bounds.minLatitude + bounds.maxLatitude) / 2
    ];
    const span = Math.max(bounds.maxLongitude - bounds.minLongitude, bounds.maxLatitude - bounds.minLatitude);
    const zoom = span > 0.18 ? 10 : span > 0.08 ? 11 : span > 0.035 ? 12 : span > 0.015 ? 13 : 14;
    if (typeof map.setZoomAndCenter === "function") {
      map.setZoomAndCenter(zoom, center, true, 0);
      return;
    }
    if (typeof map.setCenter === "function") {
      map.setCenter(center, true, 0);
      return;
    }
    map.panTo?.(center);
  } catch (error) {
    console.warn("AMap fit command failed", error);
  }
}

function removeMapOverlays(overlays: AMapOverlay[]) {
  const validOverlays = overlays.filter(Boolean);
  if (!validOverlays.length) {
    return;
  }
  validOverlays.forEach((overlay) => {
    try {
      overlay.setMap?.(null);
    } catch (error) {
      console.warn("AMap overlay cleanup failed", error);
    }
  });
}

function createMapOverlay(
  Factory: AMapOverlayFactory,
  options: Record<string, unknown>,
  map: AMapInstance,
  overlayName: string
) {
  let overlay: AMapOverlay;
  try {
    overlay = new Factory(options);
  } catch (error) {
    console.warn(`AMap ${overlayName} construction failed`, error);
    return null;
  }
  try {
    overlay.setMap?.(map);
  } catch (error) {
    console.warn(`AMap ${overlayName} attach failed`, error);
    try {
      overlay.setMap?.(null);
    } catch {
      // Best-effort cleanup only; the important part is keeping React alive.
    }
    return null;
  }
  return overlay;
}

function isMappablePoi<T extends { amapId?: string | null; source?: string | null; longitude?: number | null; latitude?: number | null }>(
  poi: T
): poi is T & { longitude: number; latitude: number } {
  return (
    typeof poi.longitude === "number" &&
    Number.isFinite(poi.longitude) &&
    typeof poi.latitude === "number" &&
    Number.isFinite(poi.latitude)
  );
}

function isMappableSegment(segment: PlannerSegment): segment is PlannerSegment & {
  poi: PlannerPoi & { longitude: number; latitude: number };
} {
  return isMappablePoi(segment.poi);
}

export function mapPoiFromSegment(
  segment: PlannerSegment & { poi: PlannerPoi & { longitude: number; latitude: number } }
): MapPoi {
  return {
    id: `timeline-${segment.id}-${segment.poi.id}`,
    amapId: segment.poi.amapId,
    name: segment.poi.name,
    type: segment.poi.type ?? segment.poi.category,
    city: segment.poi.city,
    district: segment.poi.district ?? "",
    address: segment.poi.address ?? "",
    longitude: segment.poi.longitude,
    latitude: segment.poi.latitude,
    category: segment.poi.category,
    source: segment.poi.source,
    sourceNote: segment.poi.sourceNote ?? "来源：当前时间轴地点",
    sourceUrl: segment.poi.sourceUrl,
    confidence: segment.poi.confidence,
    providerTypeCode: segment.poi.providerTypeCode,
    tags: segment.poi.tags ? [...segment.poi.tags] : [],
    sourceClaims: segment.poi.sourceClaims?.map((claim) => ({ ...claim })) ?? [],
    photos: segment.poi.photoUrl ? [{ title: segment.poi.name, url: segment.poi.photoUrl }] : []
  };
}

function uniquePois(pois: MapPoi[]) {
  const seen = new Set<string>();
  const result: MapPoi[] = [];
  for (const poi of pois) {
    const key = poi.id || `${poi.name}-${poi.longitude}-${poi.latitude}`;
    if (seen.has(key)) {
      continue;
    }
    seen.add(key);
    result.push(poi);
  }
  return result;
}

function pendingCandidateIdForPoi(candidates: PendingPoiCandidate[], poi: MapPoi, segmentId?: string | null) {
  const matches = candidates.filter((candidate) => {
    if (segmentId && candidate.sourceSegmentId && candidate.sourceSegmentId !== segmentId) {
      return false;
    }
    return candidate.candidates.some((item) => sameMapPoi(item, poi));
  });
  return matches.length === 1 ? matches[0].id : null;
}

function activePendingCandidateIdForPoi(snapshot: ReturnType<typeof plannerStore.getSnapshot>, poi: MapPoi) {
  const scopedId = snapshot.activeDensityMapComparison?.candidateRecordId ?? null;
  if (scopedId) {
    const scopedCandidate = snapshot.pendingPoiCandidates.find(
      (candidate) => candidate.id === scopedId
    );
    return scopedCandidate?.candidates.some((item) => sameMapPoi(item, poi))
      ? scopedCandidate.id
      : null;
  }
  return pendingCandidateIdForPoi(
    snapshot.pendingPoiCandidates,
    poi,
    snapshot.selectedSegmentId
  );
}

function sameMapPoi(left: MapPoi, right: MapPoi) {
  if (left.amapId && right.amapId && left.amapId === right.amapId) {
    return true;
  }
  if (left.id && right.id && left.id === right.id) {
    return true;
  }
  return (
    left.name === right.name &&
    Math.abs(left.longitude - right.longitude) < 0.000001 &&
    Math.abs(left.latitude - right.latitude) < 0.000001
  );
}

function escapeHtml(value: string) {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function lngLatToTuple(value?: AMapLngLat): [number, number] | null {
  if (!value) {
    return null;
  }
  const longitude = typeof value.getLng === "function" ? value.getLng() : value.lng;
  const latitude = typeof value.getLat === "function" ? value.getLat() : value.lat;
  if (typeof longitude !== "number" || typeof latitude !== "number") {
    return null;
  }
  return [longitude, latitude];
}

function clickedPoiHint(event?: AMapMapEvent) {
  const explicitPoi = event?.poi ?? event?.pois?.[0] ?? null;
  return {
    name: explicitPoi?.name?.trim() ?? "",
    location: explicitPoi?.location ? lngLatToTuple(explicitPoi.location) : null
  };
}

function nearestReasonablePoi(pois: MapPoi[], clickPosition: [number, number], hasExplicitName: boolean, keyword: string) {
  const ranked = pois
    .map((poi) => ({
      poi,
      distanceMeters:
        typeof poi.distanceMeters === "number"
          ? poi.distanceMeters
          : distanceMeters(clickPosition, [poi.longitude, poi.latitude])
    }))
    .sort((left, right) => left.distanceMeters - right.distanceMeters);
  const nearest = ranked[0];
  if (!nearest) {
    return null;
  }

  const normalizedKeyword = keyword.trim();
  const nameMatched = normalizedKeyword
    ? normalizedPoiName(nearest.poi.name).includes(normalizedPoiName(normalizedKeyword)) ||
      normalizedPoiName(normalizedKeyword).includes(normalizedPoiName(nearest.poi.name))
    : true;
  const maxDistance = hasExplicitName ? 180 : 120;
  if (nearest.distanceMeters > maxDistance) {
    return null;
  }
  if (hasExplicitName && !nameMatched) {
    return null;
  }
  return nearest.poi;
}

function normalizedPoiName(value: string) {
  return value.replace(/[\s·・,，。\-—_()（）]/g, "").toLowerCase();
}

function distanceMeters(from: [number, number], to: [number, number]) {
  const earthRadiusMeters = 6371000;
  const fromLatitude = degreesToRadians(from[1]);
  const toLatitude = degreesToRadians(to[1]);
  const latitudeDelta = degreesToRadians(to[1] - from[1]);
  const longitudeDelta = degreesToRadians(to[0] - from[0]);
  const a =
    Math.sin(latitudeDelta / 2) * Math.sin(latitudeDelta / 2) +
    Math.cos(fromLatitude) * Math.cos(toLatitude) * Math.sin(longitudeDelta / 2) * Math.sin(longitudeDelta / 2);
  return earthRadiusMeters * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

function displayMapDays(plan: ItineraryPlan | null, selectedDayNumber?: number | null) {
  if (!plan) {
    return [];
  }
  if (typeof selectedDayNumber !== "number") {
    return plan.days;
  }
  const selectedDay = plan.days.find((day) => day.dayNumber === selectedDayNumber);
  return selectedDay ? [selectedDay] : plan.days.slice(0, 1);
}

function mapDisplayRoutes(days: ItineraryPlan["days"], routeOptions: ItineraryPlan["routeOptions"]) {
  const groupedRoutes = new Map<string, ItineraryPlan["routeOptions"]>();
  for (const route of routeOptions) {
    if (!route.fromSegmentId || !route.toSegmentId || !route.polyline?.length || !isDisplayableRoute(route)) {
      continue;
    }
    const key = routeLegKey(route);
    groupedRoutes.set(key, [...(groupedRoutes.get(key) ?? []), route]);
  }

  const orderedRoutes: ItineraryPlan["routeOptions"] = [];
  const usedRouteIds = new Set<string>();
  for (const day of days) {
    const daySegments = day.segments.filter(isRouteAnchorSegment);
    for (let index = 0; index < daySegments.length - 1; index += 1) {
      const fromSegment = daySegments[index];
      const toSegment = daySegments[index + 1];
      const routes = groupedRoutes.get(`${fromSegment.id}->${toSegment.id}`);
      const route = routes?.find((item) => item.isSelected) ?? (routes?.length ? fastestRoute(routes) : null);
      if (route && !usedRouteIds.has(route.id)) {
        orderedRoutes.push(route);
        usedRouteIds.add(route.id);
      }
    }
  }

  return orderedRoutes;
}

function isDisplayableRoute(route: RouteOption) {
  if (route.error) {
    return false;
  }
  const status = String(route.routeStatus ?? route.status ?? route.providerPayload?.routeStatus ?? route.providerPayload?.status ?? "")
    .trim()
    .toLowerCase();
  if (status && !["verified", "selected", "ready"].includes(status)) {
    return false;
  }
  return true;
}

function routePolylineParts(routes: RouteOption[]): RoutePolylinePart[] {
  const parts: RoutePolylinePart[] = [];
  for (const route of routes) {
    const stepParts = isTransitRoute(route) ? transitStepPaths(route) : [];
    if (stepParts.length) {
      const stepCenter = (stepParts.length - 1) / 2;
      for (const [stepIndex, step] of stepParts.entries()) {
        const stepOffsetMeters = (stepIndex - stepCenter) * 2;
        parts.push({
          id: `${route.id}:step:${stepIndex}`,
          route,
          path: offsetPolyline(step.path, stepOffsetMeters),
          stepMode: step.mode,
          stepIndex
        });
      }
      continue;
    }
    const path = normalizePolylinePath(route.polyline);
    if (path.length) {
      parts.push({
        id: `${route.id}:route`,
        route,
        path,
        stepMode: route.mode || route.transportMode || "",
        stepIndex: 0
      });
    }
  }
  return parts;
}

function transitStepPaths(route: RouteOption) {
  return route.steps.flatMap((step, index) => {
    const path = polylinePathFromValue(step.polyline);
    if (!path.length) {
      return [];
    }
    return [{
      mode: typeof step.mode === "string" ? step.mode : index === 0 ? "walking" : "transit",
      path
    }];
  });
}

function isTransitRoute(route: RouteOption) {
  return (route.mode || route.transportMode) === "transit";
}

function polylinePathFromValue(value: unknown): [number, number][] {
  if (typeof value === "string") {
    return value.split(";").flatMap((item) => {
      const [longitude, latitude] = item.split(",", 2).map((part) => Number(part.trim()));
      return Number.isFinite(longitude) && Number.isFinite(latitude) ? ([[longitude, latitude] as [number, number]] as const) : [];
    });
  }
  if (Array.isArray(value)) {
    return normalizePolylinePath(value);
  }
  return [];
}

function normalizePolylinePath(value: unknown): [number, number][] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.flatMap((point) => {
    if (!Array.isArray(point) || point.length < 2) {
      return [];
    }
    const longitude = Number(point[0]);
    const latitude = Number(point[1]);
    return Number.isFinite(longitude) && Number.isFinite(latitude) ? ([[longitude, latitude] as [number, number]] as const) : [];
  });
}

function offsetPolyline(path: [number, number][], offsetMeters: number): [number, number][] {
  if (!offsetMeters) {
    return path;
  }
  const latitude = path[0]?.[1] ?? 0;
  const longitudeDegreesPerMeter = 1 / (111_320 * Math.max(0.25, Math.cos(degreesToRadians(latitude))));
  const longitudeOffset = offsetMeters * longitudeDegreesPerMeter;
  return path.map(([longitude, pointLatitude]) => [longitude + longitudeOffset, pointLatitude]);
}

function routePolylineStyle(part: RoutePolylinePart, isHighlighted: boolean, isPreview = false, routeColor?: string) {
  const stepMode = part.stepMode || part.route.mode || part.route.transportMode;
  const isWalkingStep = stepMode === "walking";
  return {
    strokeColor: routeColor ?? routeLegColor(part.route),
    strokeOpacity: isPreview ? 0.98 : isHighlighted ? 0.98 : isWalkingStep ? 0.62 : 0.76,
    strokeWeight: isPreview ? 9 : isHighlighted ? 8 : isWalkingStep ? 4 : 6,
    strokeStyle: isWalkingStep ? "dashed" : "solid",
    lineJoin: "round",
    showDir: true,
    zIndex: isPreview ? 90 : isHighlighted ? 70 : 50 + part.stepIndex
  };
}

function isHighlightedRoute(route: RouteOption, selectedRouteOptionId?: string | null) {
  return route.id === selectedRouteOptionId || (!selectedRouteOptionId && route.isSelected);
}

function routeEndpointName(
  route: RouteOption,
  segmentsById: Map<string, PlannerSegment>,
  endpoint: "from" | "to"
) {
  const segmentId = endpoint === "from" ? route.fromSegmentId : route.toSegmentId;
  const poiId = endpoint === "from" ? route.fromPoiId : route.toPoiId;
  return segmentsById.get(segmentId ?? "")?.poi.name ?? segmentsByPoiId(segmentsById).get(poiId)?.poi.name ?? "地点";
}

function segmentsByPoiId(segmentsById: Map<string, PlannerSegment>) {
  const result = new Map<string, PlannerSegment>();
  segmentsById.forEach((segment) => result.set(segment.poi.id, segment));
  return result;
}

function nextSegmentAfterMapRemoval(before: ItineraryPlan, after: ItineraryPlan, removedSegmentId: string) {
  const beforeDay = before.days.find((day) => day.segments.some((segment) => segment.id === removedSegmentId));
  const removedIndex = beforeDay?.segments.findIndex((segment) => segment.id === removedSegmentId) ?? -1;
  const candidateIds = [
    beforeDay?.segments[removedIndex + 1]?.id,
    beforeDay?.segments[removedIndex - 1]?.id,
    before.days.flatMap((day) => day.segments).find((segment) => segment.id !== removedSegmentId)?.id
  ].filter(Boolean);
  const remainingSegments = after.days.flatMap((day) => day.segments);
  for (const candidateId of candidateIds) {
    const match = remainingSegments.find((segment) => segment.id === candidateId);
    if (match) {
      return match;
    }
  }
  return remainingSegments[0] ?? null;
}

function dayNumberForMapSegment(plan: ItineraryPlan, segmentId: string | null) {
  if (!segmentId) {
    return null;
  }
  return plan.days.find((day) => day.segments.some((segment) => segment.id === segmentId))?.dayNumber ?? null;
}

function segmentExistsInMapPlan(plan: ItineraryPlan, segmentId: string | null) {
  if (!segmentId) {
    return false;
  }
  return plan.days.some((day) => day.segments.some((segment) => segment.id === segmentId));
}

function isVersionConflict(error: unknown) {
  return error instanceof ApiError && error.status === 409;
}

function isMealMapPoi(poi: MapPoi) {
  return /餐饮|餐厅|饭店|中餐|西餐|小吃|美食|咖啡|茶饮|火锅|寿司|烧烤/i.test(
    `${poi.name ?? ""} ${poi.type ?? ""} ${poi.category ?? ""}`
  );
}

function fastestRoute(routes: ItineraryPlan["routeOptions"]) {
  return [...routes].sort((left, right) => {
    const durationDiff = routeDurationSeconds(left) - routeDurationSeconds(right);
    if (durationDiff !== 0) {
      return durationDiff;
    }
    const costDiff = (left.costAmount ?? left.costEstimate ?? 0) - (right.costAmount ?? right.costEstimate ?? 0);
    if (costDiff !== 0) {
      return costDiff;
    }
    return left.distanceMeters - right.distanceMeters;
  })[0];
}

function routeDurationSeconds(route: ItineraryPlan["routeOptions"][number]) {
  if (route.durationSeconds && route.durationSeconds > 0) {
    return route.durationSeconds;
  }
  return (route.durationMinutes ?? 0) * 60;
}

function degreesToRadians(value: number) {
  return (value * Math.PI) / 180;
}

function loadAmap(jsApiKey: string, securityJsCode?: string): Promise<AMapRuntime> {
  const win = window as unknown as Window & {
    _AMapSecurityConfig?: { securityJsCode: string };
    AMap?: AMapRuntime;
  };

  if (securityJsCode) {
    win._AMapSecurityConfig = { securityJsCode };
  }

  if (win.AMap) {
    return Promise.resolve(win.AMap);
  }

  return new Promise((resolve, reject) => {
    const existing = document.querySelector<HTMLScriptElement>("script[data-amap-sdk='trip']");
    if (existing) {
      const cleanup = () => {
        existing.removeEventListener("load", handleLoad);
        existing.removeEventListener("error", handleError);
      };
      const handleLoad = () => {
        cleanup();
        win.AMap ? resolve(win.AMap) : reject(new Error("高德 JS API 未正确初始化"));
      };
      const handleError = () => {
        cleanup();
        reject(new Error("高德 JS API 脚本加载失败"));
      };
      existing.addEventListener("load", handleLoad, { once: true });
      existing.addEventListener("error", handleError, { once: true });
      return;
    }

    const script = document.createElement("script");
    script.dataset.amapSdk = "trip";
    script.src = `https://webapi.amap.com/maps?v=2.0&key=${encodeURIComponent(jsApiKey)}`;
    script.async = true;
    const cleanup = () => {
      script.onload = null;
      script.onerror = null;
    };
    script.onload = () => {
      cleanup();
      win.AMap ? resolve(win.AMap) : reject(new Error("高德 JS API 未正确初始化"));
    };
    script.onerror = () => {
      cleanup();
      reject(new Error("高德 JS API 脚本加载失败"));
    };
    document.head.appendChild(script);
  });
}

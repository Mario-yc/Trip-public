import { type RouteOption } from "../../services/apiClient";

const ROUTE_COLOR_PALETTE = [
  { family: "blue", color: "#2563eb" },
  { family: "orange", color: "#f97316" },
  { family: "green", color: "#16a34a" },
  { family: "magenta", color: "#c026d3" },
  { family: "red", color: "#dc2626" },
  { family: "cyan", color: "#0891b2" },
  { family: "gold", color: "#ca8a04" },
  { family: "violet", color: "#7c3aed" },
  { family: "lime", color: "#65a30d" },
  { family: "slate", color: "#475569" },
  { family: "rose", color: "#e11d48" },
  { family: "teal", color: "#0f766e" }
];

type RouteDay = {
  segments: Array<{ id: string }>;
};
export type RouteColorMap = Map<string, string>;

export function routeLegKey(route: RouteOption) {
  return `${route.fromSegmentId ?? route.fromPoiId}->${route.toSegmentId ?? route.toPoiId}`;
}

export function buildRouteLegColorMap(days: RouteDay[], routeOptions: RouteOption[]): RouteColorMap {
  const colorByLeg = new Map<string, string>();
  const legKeys = new Set<string>();
  for (const route of routeOptions) {
    const key = routeSegmentLegKey(route);
    if (key) {
      legKeys.add(key);
    }
  }

  for (const day of days) {
    const segmentOrder = new Map(day.segments.map((segment, index) => [segment.id, index]));
    const dayLegKeys = [...legKeys]
      .filter((key) => {
        const [fromSegmentId, toSegmentId] = splitLegKey(key);
        return segmentOrder.has(fromSegmentId) && segmentOrder.has(toSegmentId);
      })
      .sort((left, right) => compareLegOrder(left, right, segmentOrder));
    dayLegKeys.forEach((key, index) => {
      colorByLeg.set(key, ROUTE_COLOR_PALETTE[index % ROUTE_COLOR_PALETTE.length].color);
    });
  }

  [...legKeys]
    .filter((key) => !colorByLeg.has(key))
    .sort()
    .forEach((key) => colorByLeg.set(key, ROUTE_COLOR_PALETTE[hashRouteKey(key)].color));
  return colorByLeg;
}

export function routeLegColor(route: RouteOption, routeColorMap?: RouteColorMap) {
  const hasEndpointKey = Boolean(route.fromSegmentId ?? route.fromPoiId) && Boolean(route.toSegmentId ?? route.toPoiId);
  const key = hasEndpointKey ? routeLegKey(route) : "";
  const assignedColor = key ? routeColorMap?.get(key) : undefined;
  if (assignedColor) {
    return assignedColor;
  }
  if (key) {
    return ROUTE_COLOR_PALETTE[hashRouteKey(key)].color;
  }
  const explicitOrder = numericSuffix(route.id);
  if (explicitOrder !== null) {
    return ROUTE_COLOR_PALETTE[Math.max(0, explicitOrder - 1) % ROUTE_COLOR_PALETTE.length].color;
  }
  return ROUTE_COLOR_PALETTE[hashRouteKey(key || route.id)].color;
}

function routeSegmentLegKey(route: RouteOption) {
  return route.fromSegmentId && route.toSegmentId ? `${route.fromSegmentId}->${route.toSegmentId}` : "";
}

function splitLegKey(key: string) {
  return key.split("->", 2) as [string, string];
}

function compareLegOrder(left: string, right: string, segmentOrder: Map<string, number>) {
  const [leftFrom, leftTo] = splitLegKey(left);
  const [rightFrom, rightTo] = splitLegKey(right);
  const fromDiff = (segmentOrder.get(leftFrom) ?? Number.MAX_SAFE_INTEGER) - (segmentOrder.get(rightFrom) ?? Number.MAX_SAFE_INTEGER);
  if (fromDiff !== 0) {
    return fromDiff;
  }
  return (segmentOrder.get(leftTo) ?? Number.MAX_SAFE_INTEGER) - (segmentOrder.get(rightTo) ?? Number.MAX_SAFE_INTEGER);
}

function hashRouteKey(key: string) {
  let hash = 2166136261;
  for (let index = 0; index < key.length; index += 1) {
    hash ^= key.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return Math.abs(hash) % ROUTE_COLOR_PALETTE.length;
}

function numericSuffix(value?: string | null) {
  const match = value?.match(/(\d+)$/);
  return match ? Number(match[1]) : null;
}

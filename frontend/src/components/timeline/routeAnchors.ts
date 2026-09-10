type SegmentPoiLike = {
  routeable?: boolean;
  groundingStatus?: string | null;
  sourceNote?: string | null;
  needsConcretePoi?: boolean | null;
  grounding?: {
    routeable?: boolean;
    groundingStatus?: string | null;
    status?: string | null;
    needsConcretePoi?: boolean | null;
  } | null;
};

type RouteAnchorSegmentLike = {
  kind: string;
  routeable?: boolean;
  groundingStatus?: string | null;
  needsConcretePoi?: boolean | null;
  notes?: string | null;
  sourceNote?: string | null;
  poi?: SegmentPoiLike;
};

const NON_ROUTE_GROUNDING_STATUSES = new Set([
  "not_required",
  "draft_only",
  "waiting_for_poi_grounding",
  "area_unresolved",
  "provider_rate_limited",
  "area_poi",
  "functional_poi",
  "composite_poi"
]);

export function isRouteAnchorSegment(segment: RouteAnchorSegmentLike) {
  const status = routeAnchorGroundingStatus(segment);
  const diagnosticText = routeAnchorDiagnosticText(segment);
  const needsConcretePoi =
    Boolean(segment.needsConcretePoi ?? segment.poi?.needsConcretePoi ?? segment.poi?.grounding?.needsConcretePoi) ||
    /needsConcretePoi\s*[=:：]\s*true/i.test(diagnosticText);
  const nonRouteStatus = NON_ROUTE_GROUNDING_STATUSES.has(status);
  const explicitlyNonRoute = /routeAnchor\s*[=:：]\s*false/i.test(diagnosticText);
  const routeable = segment.routeable ?? segment.poi?.routeable ?? segment.poi?.grounding?.routeable;
  if (segment.kind === "visit" || segment.kind === "activity") {
    return !needsConcretePoi && !nonRouteStatus && !explicitlyNonRoute;
  }
  if (segment.kind !== "meal") {
    return false;
  }
  return Boolean(routeable) && !needsConcretePoi && !nonRouteStatus && !explicitlyNonRoute;
}

function routeAnchorGroundingStatus(segment: RouteAnchorSegmentLike) {
  return String(
    segment.groundingStatus ??
      segment.poi?.groundingStatus ??
      segment.poi?.grounding?.groundingStatus ??
      segment.poi?.grounding?.status ??
      ""
  );
}

function routeAnchorDiagnosticText(segment: RouteAnchorSegmentLike) {
  return [
    segment.groundingStatus,
    segment.notes,
    segment.sourceNote,
    segment.poi?.groundingStatus,
    segment.poi?.sourceNote,
    segment.poi?.grounding?.groundingStatus,
    segment.poi?.grounding?.status
  ]
    .filter(Boolean)
    .join(" ");
}

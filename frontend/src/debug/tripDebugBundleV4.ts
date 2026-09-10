import type { TripTestBundleSnapshot } from "./tripTestBundle";

export const TRIP_DEBUG_BUNDLE_VERSION = 4;
export const DEBUG_SECTION_ORDER = [
  "META",
  "COMPLETENESS",
  "CONVERSATION_TURNS",
  "EVENT_STORE",
  "TURN_EVENT_REFERENCES",
  "AGENT_REQUEST_CONTEXTS",
  "AGENT_RESPONSES",
  "ERRORS",
  "CONTROLLER_RUNS",
  "PLANNING_RUNS",
  "PLANNING_TRACE",
  "STRUCTURED_CHOICES",
  "CHOICE_EXECUTIONS",
  "EXPLORATION_FRONTIERS",
  "PORTFOLIOS",
  "PROPOSALS",
  "PROPOSAL_SCORES",
  "PROPOSAL_VERIFIERS",
  "PROPOSAL_LINEAGE",
  "COMPARISON_STATE",
  "MAP_STATE",
  "ACTIVE_ITINERARY",
  "ITINERARY_VERSIONS",
  "PATCHES",
  "TIMELINE_MUTATION_TRANSACTIONS",
  "ROUTE_EVIDENCE",
  "PENDING_SLOTS",
  "ONLINE_ENRICHMENT",
  "PROVIDER_STATUS",
  "FRONTEND_UI_STATE"
] as const;

export type TripDebugBundleV4 = { schemaVersion: "trip-debug-bundle-v4"; sections: Record<string, unknown> };
export type DebugBundleDelivery = "copied" | "downloaded";

export function buildTripDebugBundleObject(snapshot: TripTestBundleSnapshot): TripDebugBundleV4 {
  const server = asRecord(snapshot.serverDebugBundle);
  const serverSections = asRecord(server.sections);
  if (server.schemaVersion === "trip-debug-bundle-v4" && Object.keys(serverSections).length) {
    const sections = redact(serverSections) as Record<string, unknown>;
    sections.META = {
      ...asRecord(sections.META),
      capturedAt: snapshot.capturedAt,
      activeVersionId: snapshot.activeVersionId,
      bundleVersion: 4
    };
    sections.FRONTEND_UI_STATE = frontendState(snapshot);
    return { schemaVersion: "trip-debug-bundle-v4", sections };
  }
  const turns = Array.isArray(snapshot.turns) ? snapshot.turns : [];
  const normalizedTurns = turns.map((rawTurn) => {
    const { planningSteps: _planningSteps, toolEvents: _toolEvents, ...turn } = asRecord(rawTurn);
    return turn;
  });
  const eventStore: Record<string, unknown> = {};
  const turnRefs = turns.map((rawTurn, turnIndex) => {
    const turn = asRecord(rawTurn);
    const events = [
      ...(Array.isArray(turn.planningSteps) ? turn.planningSteps : []),
      ...(Array.isArray(turn.toolEvents) ? turn.toolEvents : [])
    ];
    const eventIds = events.map((rawEvent, eventIndex) => {
      const event = asRecord(rawEvent);
      const metadata = asRecord(event.metadata);
      const stableOrdinal = event.sequence ?? metadata.cycleIndex ?? eventIndex;
      const id = String(
        event.id ??
          event.eventId ??
          `turn_${turn.id ?? turnIndex}_event_${stableOrdinal}_${event.type ?? "unknown"}_${event.timestamp ?? ""}`
      );
      eventStore[id] ??= redact(event);
      return id;
    });
    return { turnId: turn.id ?? turn.turnId ?? null, eventIds: [...new Set(eventIds)] };
  });
  const comparisonPlans = asRecord(snapshot.comparisonState).plans;
  const counts = {
    conversationTurnCount: turns.length,
    eventCount: Object.keys(eventStore).length,
    proposalCount: Array.isArray(comparisonPlans) ? comparisonPlans.length : 0,
    choiceExecutionCount: 0,
    versionCount: snapshot.activeVersionId ? 1 : 0
  };
  const unavailable = (reason = "local_fallback") => ({ status: "unavailable", reason });
  const sections: Record<string, unknown> = {
    META: {
      capturedAt: snapshot.capturedAt,
      sessionId: asRecord(snapshot.session).sessionId ?? asRecord(snapshot.session).id ?? null,
      activeVersionId: snapshot.activeVersionId,
      bundleVersion: 4
    },
    CONVERSATION_TURNS: redact(normalizedTurns),
    EVENT_STORE: eventStore,
    TURN_EVENT_REFERENCES: turnRefs,
    AGENT_REQUEST_CONTEXTS: { status: "local_fallback", session: redact(snapshot.session) },
    AGENT_RESPONSES: unavailable(),
    ERRORS: unavailable(),
    CONTROLLER_RUNS: redact(snapshot.planningProcess),
    PLANNING_RUNS: redact(snapshot.planningProcess),
    PLANNING_TRACE: { eventIds: Object.keys(eventStore), eventStoreRef: "EVENT_STORE" },
    STRUCTURED_CHOICES: unavailable(),
    CHOICE_EXECUTIONS: unavailable(),
    EXPLORATION_FRONTIERS: unavailable(),
    PORTFOLIOS: unavailable(),
    PROPOSALS: redact(Array.isArray(comparisonPlans) ? comparisonPlans : []),
    PROPOSAL_SCORES: unavailable(),
    PROPOSAL_VERIFIERS: unavailable(),
    PROPOSAL_LINEAGE: unavailable(),
    COMPARISON_STATE: redact(snapshot.comparisonState ?? null),
    MAP_STATE: redact(snapshot.mapInteraction ?? null),
    ACTIVE_ITINERARY: redact(snapshot.itinerary ?? null),
    ITINERARY_VERSIONS: unavailable(),
    PATCHES: unavailable(),
    TIMELINE_MUTATION_TRANSACTIONS: unavailable(),
    ROUTE_EVIDENCE: redact(asRecord(snapshot.itinerary).routeOptions ?? []),
    PENDING_SLOTS: redact(asRecord(snapshot.itinerary).portfolioPendingSlots ?? []),
    ONLINE_ENRICHMENT: redact(asRecord(snapshot.itinerary).onlineEnrichment ?? null),
    PROVIDER_STATUS: unavailable("credentials_intentionally_excluded"),
    FRONTEND_UI_STATE: frontendState(snapshot)
  };
  sections.COMPLETENESS = {
    truncated: false,
    captureSource: "local_fallback",
    expected: counts,
    exported: counts,
    serverRefresh: unavailable(snapshot.sessionCaptureError ?? "server_debug_bundle_unavailable"),
    sections: Object.fromEntries(
      DEBUG_SECTION_ORDER.map((name) => [
        name,
        { status: asRecord(sections[name]).status === "unavailable" ? "unavailable" : "complete" }
      ])
    )
  };
  return { schemaVersion: "trip-debug-bundle-v4", sections };
}

export function buildTripTestBundle(snapshot: TripTestBundleSnapshot): string {
  const bundle = buildTripDebugBundleObject(snapshot);
  return [
    `TRIP_DEBUG_BUNDLE_VERSION=${TRIP_DEBUG_BUNDLE_VERSION}`,
    ...DEBUG_SECTION_ORDER.flatMap((name) => [
      `=== ${name} ===`,
      JSON.stringify(bundle.sections[name] ?? { status: "unavailable" }, null, 2)
    ]),
    "END_TRIP_DEBUG_BUNDLE"
  ].join("\n");
}

export function buildTripDebugBundleJson(snapshot: TripTestBundleSnapshot): string {
  return JSON.stringify(buildTripDebugBundleObject(snapshot), null, 2);
}

export async function deliverTripDebugBundle(args: {
  text: string;
  json: string;
  filename: string;
  copy: (value: string) => Promise<void>;
  download: (filename: string, value: string) => void;
}): Promise<DebugBundleDelivery> {
  try {
    await args.copy(args.text);
    return "copied";
  } catch {
    args.download(args.filename, args.json);
    try {
      await args.copy(`完整 Agent 调试包已下载：${args.filename}\n剪贴板仅包含此索引；文件内容未截断。`);
    } catch {
      /* download is authoritative */
    }
    return "downloaded";
  }
}

function frontendState(snapshot: TripTestBundleSnapshot): unknown {
  return redact({
    status: "complete",
    comparisonState: snapshot.comparisonState ?? null,
    mapInteraction: snapshot.mapInteraction ?? null,
    planningProcess: snapshot.planningProcess ?? null,
    pendingPlanningStep: snapshot.pendingPlanningStep ?? null,
    timelineText: snapshot.timelineText,
    visibleError: snapshot.visibleError ?? null,
    modelDisplayName: snapshot.modelDisplayName ?? null
  });
}

function asRecord(value: unknown): Record<string, any> {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, any>) : {};
}
function redact(value: unknown, key = ""): unknown {
  if (
    /(api.?key|authorization|cookie|(?:access|refresh|id)?token$|secret|securityJsCode|credential|password|manualValue|^sig(?:nature)?$|(?:^|[-_])(?:request|auth|credential)[-_]sig(?:nature)?$|x.?amz.?(?:credential|signature)|^reasoning$|reasoning_content|reasoningText|chainOfThought)/i.test(
      key
    )
  )
    return "[REDACTED]";
  if (Array.isArray(value)) return value.map((item) => redact(item, key));
  if (value !== null && typeof value === "object")
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>).map(([childKey, item]) => [childKey, redact(item, childKey)])
    );
  if (typeof value !== "string") return value;
  return value
    .replace(/bearer\s+[A-Za-z0-9._~+/=-]+/gi, "Bearer [REDACTED]")
    .replace(
      /([?&#](?:api[_-]?key|(?:access|refresh|id)[_-]?token|token|key|auth(?:orization)?|cookie|client[_-]?secret|secret|sig(?:nature)?|x-amz-(?:credential|signature)|securityJsCode)=)[^&#\s]+/gi,
      "$1[REDACTED]"
    )
    .replace(/\b[A-Z]:[\\/]+(?:[^\\/\s\"'<>|]+[\\/]+)*[^\\/\s\"'<>|]+/gi, "[LOCAL_PATH]")
    .replace(
      /(?<![:A-Za-z0-9_])(?:\\\\+[^\\\s\"'<>|]+\\+[^\\\s\"'<>|]+(?:\\+[^\\\s\"'<>|]+)*|\/\/+[^/\s\"'<>]+\/[^/\s\"'<>]+(?:\/[^/\s\"'<>]+)*)/gi,
      "[LOCAL_PATH]"
    )
    .replace(
      /(?<![:/A-Za-z0-9_\u4e00-\u9fff])\/(?!\/)(?:(?:home|Users|tmp|var|private|opt|srv|mnt|Volumes|workspace|root)(?:\/[^/\s\"'<>]+)+|[^/\s\"'<>]+(?:\/[^/\s\"'<>]+){2,})/g,
      "[LOCAL_PATH]"
    );
}

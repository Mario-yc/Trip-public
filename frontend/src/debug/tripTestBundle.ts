const SECTION_ORDER = ["TEST META", "CONVERSATION", "STRUCTURED CHOICE REQUESTS", "CHOICE EXECUTIONS", "PLANNING TRACE", "CONTROLLER PERFORMANCE", "PORTFOLIO VISIBILITY", "COMPARISON STATE", "MAP INTERACTION", "TIMELINE MUTATION TRANSACTIONS", "VERIFIER", "TIMELINE", "RAW DEBUG"] as const;
export { buildTripTestBundle, buildTripDebugBundleJson, buildTripDebugBundleObject, deliverTripDebugBundle } from "./tripDebugBundleV4";
const MAX_BUNDLE_BYTES = 500 * 1024;
const RAW_DEBUG_TRUNCATION_MARKER = "[TRUNCATED_TO_FIT_500KB_BUNDLE_LIMIT]";

export type TripTestBundleSnapshot = {
  capturedAt: string;
  session: unknown;
  activeVersionId: string | null;
  turns: unknown;
  planningProcess: unknown;
  pendingPlanningStep: unknown;
  itinerary: unknown;
  timelineText: string;
  sessionCaptureSource?: "server_refresh" | "local_fallback";
  sessionCaptureError?: string | null;
  comparisonState?: unknown;
  mapInteraction?: unknown;
  serverDebugBundle?: unknown;
  visibleError?: string | null;
  modelDisplayName?: string | null;
};

export function buildTripTestBundleV3(snapshot: TripTestBundleSnapshot): string {
  const itinerary = asRecord(snapshot.itinerary);
  const sections = stripTraceOnlyEvidence({
    "TEST META": {
      capturedAt: snapshot.capturedAt,
      sessionId: asRecord(snapshot.session).sessionId ?? asRecord(snapshot.session).id ?? null,
      activeVersionId: snapshot.activeVersionId,
      bundleVersion: 3,
      sessionCaptureSource: snapshot.sessionCaptureSource ?? "local_fallback",
      sessionCaptureError: snapshot.sessionCaptureError ?? null
    },
    CONVERSATION: conversationWithoutTrace(snapshot.turns),
    "STRUCTURED CHOICE REQUESTS": structuredChoiceRequests(snapshot.turns),
    "CHOICE EXECUTIONS": choiceExecutions(snapshot.turns),
    "PLANNING TRACE": planningTrace(snapshot),
    "CONTROLLER PERFORMANCE": controllerPerformance(snapshot.turns),
    "PORTFOLIO VISIBILITY": portfolioVisibility(snapshot),
    "COMPARISON STATE": snapshot.comparisonState ?? null,
    "MAP INTERACTION": snapshot.mapInteraction ?? null,
    "TIMELINE MUTATION TRANSACTIONS": timelineMutationTransactions(snapshot.turns),
    VERIFIER: {
      routeCoverage: itinerary.routeCoverage ?? null,
      scheduleDiagnostics: itinerary.scheduleDiagnostics ?? null,
      onlineEnrichment: itinerary.onlineEnrichment ?? null,
      budgetBreakdown: itinerary.budgetBreakdown ?? null
    },
    TIMELINE: snapshot.timelineText || "(empty)",
    "RAW DEBUG": redact({
      session: snapshot.session,
      activeVersionId: snapshot.activeVersionId,
      itinerary: snapshot.itinerary,
      refs: {
        conversation: "CONVERSATION",
        planningTrace: "PLANNING TRACE",
        timeline: "TIMELINE"
      }
    })
  }) as Record<(typeof SECTION_ORDER)[number], unknown>;
  let bundle = assembleBundle(sections);
  if (clipboardUtf8Size(bundle) <= MAX_BUNDLE_BYTES) return bundle;

  sections["RAW DEBUG"] = {
    truncated: true,
    reason: RAW_DEBUG_TRUNCATION_MARKER,
    originalBundleBytes: clipboardUtf8Size(bundle),
    refs: { conversation: "CONVERSATION", planningTrace: "PLANNING TRACE", timeline: "TIMELINE" }
  };
  bundle = assembleBundle(sections);
  if (clipboardUtf8Size(bundle) <= MAX_BUNDLE_BYTES) return bundle;

  sections["PLANNING TRACE"] = compactPlanningTrace(sections["PLANNING TRACE"]);
  sections.CONVERSATION = compactConversation(sections.CONVERSATION);
  sections.TIMELINE = compactTimeline(sections.TIMELINE);
  sections.VERIFIER = compactVerifier(sections.VERIFIER);
  sections["COMPARISON STATE"] = compactComparisonState(sections["COMPARISON STATE"]);
  sections["TIMELINE MUTATION TRANSACTIONS"] = compactMutationTransactions(
    sections["TIMELINE MUTATION TRANSACTIONS"]
  );
  bundle = assembleBundle(sections);
  if (clipboardUtf8Size(bundle) <= MAX_BUNDLE_BYTES) return bundle;

  sections.CONVERSATION = compactConversation(sections.CONVERSATION, true);
  sections["STRUCTURED CHOICE REQUESTS"] = compactStructuredChoices(
    sections["STRUCTURED CHOICE REQUESTS"]
  );
  sections["CHOICE EXECUTIONS"] = compactChoiceExecutions(sections["CHOICE EXECUTIONS"]);
  sections["CONTROLLER PERFORMANCE"] = compactControllerPerformance(
    sections["CONTROLLER PERFORMANCE"]
  );
  sections["PORTFOLIO VISIBILITY"] = compactPortfolioVisibility(sections["PORTFOLIO VISIBILITY"]);
  sections["MAP INTERACTION"] = compactMapInteraction(sections["MAP INTERACTION"]);
  sections["PLANNING TRACE"] = {
    truncated: true,
    reason: RAW_DEBUG_TRUNCATION_MARKER,
    eventCount: asRecord(sections["PLANNING TRACE"]).eventCount ?? null
  };
  sections.TIMELINE = RAW_DEBUG_TRUNCATION_MARKER;
  bundle = assembleBundle(sections);
  if (clipboardUtf8Size(bundle) <= MAX_BUNDLE_BYTES) return bundle;

  sections.CONVERSATION = [];
  sections["STRUCTURED CHOICE REQUESTS"] = compactStructuredChoices(
    sections["STRUCTURED CHOICE REQUESTS"], 5, 5, 128
  );
  sections["CHOICE EXECUTIONS"] = compactChoiceExecutions(sections["CHOICE EXECUTIONS"], 5, 5, 128);
  sections["CONTROLLER PERFORMANCE"] = compactControllerPerformance(
    sections["CONTROLLER PERFORMANCE"],
    20
  );
  sections["TIMELINE MUTATION TRANSACTIONS"] = compactMutationTransactions(
    sections["TIMELINE MUTATION TRANSACTIONS"]
  ).slice(-25);
  sections.VERIFIER = compactVerifier(sections.VERIFIER);
  bundle = assembleBundle(sections);
  if (clipboardUtf8Size(bundle) > MAX_BUNDLE_BYTES) {
    throw new Error("TRIP_TEST_BUNDLE_V3 could not preserve complete sections within 500KB");
  }
  return bundle;
}

function assembleBundle(sections: Record<(typeof SECTION_ORDER)[number], unknown>): string {
  return [
    "TRIP_TEST_BUNDLE_VERSION=3",
    ...SECTION_ORDER.flatMap((name) => [`=== ${name} ===`, serialize(sections[name])]),
    "END_TRIP_TEST_BUNDLE"
  ].join("\n");
}

function compactPlanningTrace(value: unknown): Record<string, unknown> {
  const trace = asRecord(value);
  const runs = asRecord(trace.runs);
  return {
    truncated: true,
    reason: RAW_DEBUG_TRUNCATION_MARKER,
    originalBytes: utf8Size(serialize(value)),
    eventCount: trace.eventCount ?? null,
    runKeys: Object.keys(runs),
    pendingPlanningStep: trace.pendingPlanningStep ?? null
  };
}

function compactConversation(value: unknown, minimal = false): unknown[] {
  if (!Array.isArray(value)) return [];
  return value.map((item) => {
    const turn = asRecord(item);
    const content = typeof turn.content === "string" ? turn.content : "";
    return {
      id: turn.id ?? turn.turnId ?? null,
      role: turn.role ?? null,
      status: turn.status ?? null,
      createdAt: turn.createdAt ?? turn.created_at ?? null,
      contentPreview: minimal ? undefined : content.slice(0, 1_000),
      contentTruncated: content.length > (minimal ? 0 : 1_000)
    };
  });
}

function compactTimeline(value: unknown): string {
  const timeline = typeof value === "string" ? value : serialize(value);
  if (utf8Size(timeline) <= 32 * 1024) return timeline;
  return `${truncateUtf8(timeline, 32 * 1024)}\n${RAW_DEBUG_TRUNCATION_MARKER}`;
}

function compactVerifier(value: unknown): Record<string, unknown> {
  const verifier = asRecord(value);
  return Object.fromEntries(
    Object.entries(verifier).map(([key, item]) => {
      const record = asRecord(item);
      return [key, Object.keys(record).length ? { available: true, keys: Object.keys(record) } : item ?? null];
    })
  );
}

function compactComparisonState(value: unknown): Record<string, unknown> {
  const comparison = asRecord(value);
  const plans = Array.isArray(comparison.plans) ? comparison.plans.slice(-100).map(asRecord) : [];
  return {
    truncated: true,
    reason: RAW_DEBUG_TRUNCATION_MARKER,
    planningSelectionRootTurnId: comparison.planningSelectionRootTurnId ?? null,
    rootPortfolioId: comparison.rootPortfolioId ?? null,
    focusedProposalId: comparison.focusedProposalId ?? null,
    adoptedProposalId: comparison.adoptedProposalId ?? null,
    plans: plans.map((plan) => ({
      proposalId: plan.proposalId ?? null,
      rootPortfolioId: plan.rootPortfolioId ?? null,
      planningSelectionRootTurnId: plan.planningSelectionRootTurnId ?? null,
      status: plan.status ?? null,
      isPartial: plan.isPartial === true,
      isAdopted: plan.isAdopted === true,
      adoptionReady: plan.adoptionReady ?? null,
      title: shortText(plan.title)
    }))
  };
}

function compactMutationTransactions(value: unknown): unknown[] {
  if (!Array.isArray(value)) return [];
  return value.map((item) => {
    const transaction = asRecord(item);
    return {
      mutationId: transaction.mutationId ?? null,
      status: transaction.status ?? null,
      sourceTurnId: transaction.sourceTurnId ?? null,
      baseVersionId: transaction.baseVersionId ?? null,
      resultVersionId: transaction.resultVersionId ?? null,
      patchId: transaction.patchId ?? null,
      rollbackPerformed: transaction.rollbackPerformed ?? null,
      postconditionPassed: transaction.postconditionPassed ?? asRecord(transaction.postconditionVerifier).passed ?? null
    };
  });
}

function compactStructuredChoices(value: unknown, limit = 100, arrayLimit = 100, textLimit = 512): unknown[] {
  if (!Array.isArray(value)) return [];
  return value.slice(-limit).map((item) => {
    const trace = asRecord(item);
    return {
      sourceAssistantTurnId: shortText(trace.sourceAssistantTurnId, textLimit),
      requestChoiceId: shortText(trace.requestChoiceId, textLimit),
      persistedChoiceId: shortText(trace.persistedChoiceId, textLimit),
      persistedChoiceAction: shortText(trace.persistedChoiceAction, textLimit),
      resolvedChoiceId: shortText(trace.resolvedChoiceId, textLimit),
      executionId: shortText(trace.executionId, textLimit),
      executionStatus: shortText(trace.executionStatus, textLimit),
      planningSelectionRootTurnId: shortText(trace.planningSelectionRootTurnId, textLimit),
      rootPortfolioId: shortText(trace.rootPortfolioId, textLimit),
      requestContractFingerprint: shortText(trace.requestContractFingerprint, textLimit),
      expectedBaseVersionId: shortText(trace.expectedBaseVersionId, textLimit),
      outcome: compactChoiceOutcome(trace.outcome, arrayLimit, textLimit),
      versionDelta: trace.versionDelta ?? null,
      patchDelta: trace.patchDelta ?? null,
      routeWriteDelta: trace.routeWriteDelta ?? null
    };
  });
}

function compactChoiceExecutions(value: unknown, limit = 100, arrayLimit = 100, textLimit = 512): unknown[] {
  if (!Array.isArray(value)) return [];
  return value.slice(-limit).map((item) => {
    const execution = asRecord(item);
    return {
      executionId: shortText(execution.executionId, textLimit),
      executionStatus: shortText(execution.executionStatus, textLimit),
      action: shortText(execution.action, textLimit),
      sourceAssistantTurnId: shortText(execution.sourceAssistantTurnId, textLimit),
      requestChoiceId: shortText(execution.requestChoiceId, textLimit),
      persistedChoiceId: shortText(execution.persistedChoiceId, textLimit),
      planningSelectionRootTurnId: shortText(execution.planningSelectionRootTurnId, textLimit),
      rootPortfolioId: shortText(execution.rootPortfolioId, textLimit),
      requestContractFingerprint: shortText(execution.requestContractFingerprint, textLimit),
      expectedBaseVersionId: shortText(execution.expectedBaseVersionId, textLimit),
      resultVersionId: shortText(execution.resultVersionId, textLimit),
      outcome: compactChoiceOutcome(execution.outcome, arrayLimit, textLimit),
      versionDelta: execution.versionDelta ?? null,
      patchDelta: execution.patchDelta ?? null,
      routeWriteDelta: execution.routeWriteDelta ?? null
    };
  });
}

function compactChoiceOutcome(value: unknown, arrayLimit = 100, textLimit = 512): Record<string, unknown> | null {
  const outcome = asRecord(value);
  if (!Object.keys(outcome).length) return null;
  return {
    succeeded: outcome.succeeded ?? null,
    failureClass: shortText(outcome.failureClass, textLimit),
    proposalDelta: outcome.proposalDelta ?? null,
    proposalIdsBefore: shortStringArray(outcome.proposalIdsBefore, arrayLimit, textLimit),
    proposalIdsAfter: shortStringArray(outcome.proposalIdsAfter, arrayLimit, textLimit),
    addedProposalIds: shortStringArray(outcome.addedProposalIds, arrayLimit, textLimit),
    planningSelectionRootTurnId: shortText(outcome.planningSelectionRootTurnId, textLimit),
    rootPortfolioId: shortText(outcome.rootPortfolioId, textLimit),
    requestContractFingerprint: shortText(outcome.requestContractFingerprint, textLimit),
    versionDelta: outcome.versionDelta ?? null,
    patchDelta: outcome.patchDelta ?? null,
    routeWriteDelta: outcome.routeWriteDelta ?? null
  };
}

function compactControllerPerformance(value: unknown, limit = 100): unknown[] {
  if (!Array.isArray(value)) return [];
  return value.slice(-limit).map((item) => {
    const evidence = asRecord(item);
    const samples = Array.isArray(evidence.samples) ? evidence.samples.slice(-20).map(asRecord) : [];
    return {
      turnId: shortText(evidence.turnId),
      eventId: shortText(evidence.eventId),
      decisionPath: shortText(evidence.decisionPath),
      controllerFailureClass: shortText(evidence.controllerFailureClass),
      samples: samples.map((sample) => ({
        callKind: shortText(sample.callKind),
        captureState: shortText(sample.captureState),
        httpStatus: sample.httpStatus ?? null,
        payloadBytes: sample.payloadBytes ?? null,
        responseBytes: sample.responseBytes ?? null,
        finishReason: shortText(sample.finishReason),
        contentLength: sample.contentLength ?? null,
        reasoningContentLength: sample.reasoningContentLength ?? null,
        tokenUsage: {
          prompt_tokens: asRecord(sample.tokenUsage).prompt_tokens ?? null,
          completion_tokens: asRecord(sample.tokenUsage).completion_tokens ?? null,
          total_tokens: asRecord(sample.tokenUsage).total_tokens ?? null
        }
      }))
    };
  });
}

function compactPortfolioVisibility(value: unknown): Record<string, unknown> {
  const visibility = asRecord(value);
  return {
    planningDirectionCount: visibility.planningDirectionCount ?? null,
    planningDirectionIds: shortStringArray(visibility.planningDirectionIds),
    verifiedComparisonProposalCount: visibility.verifiedComparisonProposalCount ?? null,
    verifiedComparisonProposalIds: shortStringArray(visibility.verifiedComparisonProposalIds),
    partialPlanCount: visibility.partialPlanCount ?? null,
    visibleCardCount: visibility.visibleCardCount ?? null,
    focusedProposalId: shortText(visibility.focusedProposalId),
    adoptedProposalId: shortText(visibility.adoptedProposalId),
    planningSelectionRootTurnId: shortText(visibility.planningSelectionRootTurnId),
    rootPortfolioId: shortText(visibility.rootPortfolioId),
    plans: Array.isArray(visibility.plans)
      ? visibility.plans.slice(-100).map((item) => {
        const plan = asRecord(item);
        return {
          proposalId: shortText(plan.proposalId),
          rootPortfolioId: shortText(plan.rootPortfolioId),
          planningSelectionRootTurnId: shortText(plan.planningSelectionRootTurnId),
          status: shortText(plan.status),
          isPartial: plan.isPartial === true,
          isAdopted: plan.isAdopted === true,
          title: shortText(plan.title)
        };
      })
      : []
  };
}

function compactMapInteraction(value: unknown): Record<string, unknown> {
  const map = asRecord(value);
  return {
    initialized: map.initialized ?? null,
    mapState: shortText(map.mapState),
    mode: shortText(map.mode),
    capabilities: {
      navigate: asRecord(map.capabilities).navigate ?? null,
      inspect: asRecord(map.capabilities).inspect ?? null,
      search: asRecord(map.capabilities).search ?? null,
      mutateItinerary: asRecord(map.capabilities).mutateItinerary ?? null,
      confirmPendingSlot: asRecord(map.capabilities).confirmPendingSlot ?? null
    },
    center: map.center ?? null,
    zoom: map.zoom ?? null,
    interactionStartCenter: map.interactionStartCenter ?? null,
    interactionStartZoom: map.interactionStartZoom ?? null,
    dragCount: map.dragCount ?? null,
    wheelCount: map.wheelCount ?? null,
    zoomCount: map.zoomCount ?? null,
    moveCount: map.moveCount ?? null,
    recentEvents: Array.isArray(map.recentEvents)
      ? map.recentEvents.slice(-50).map((item) => {
        const event = asRecord(item);
        return { type: shortText(event.type), at: shortText(event.at) };
      })
      : [],
    pointerTarget: shortText(map.pointerTarget),
    updatedAt: shortText(map.updatedAt)
  };
}

function shortText(value: unknown, limit = 512): string | null {
  if (value === null || value === undefined) return null;
  return String(value).slice(0, limit);
}

function shortStringArray(value: unknown, limit = 100, textLimit = 512): string[] {
  return Array.isArray(value) ? value.slice(-limit).map((item) => String(item).slice(0, textLimit)) : [];
}

function timelineMutationTransactions(turns: unknown): unknown[] {
  if (!Array.isArray(turns)) return [];
  const seen = new Set<string>();
  const transactions: unknown[] = [];
  for (const turn of turns) {
    const record = asRecord(turn);
    const transaction = asRecord(record.timelineMutationTransaction);
    const outcome = Object.keys(transaction).length ? transaction : asRecord(record.timelineMutationOutcome);
    if (!Object.keys(outcome).length) continue;
    const mutationId = String(outcome.mutationId ?? "");
    if (mutationId && seen.has(mutationId)) continue;
    if (mutationId) seen.add(mutationId);
    transactions.push(outcome);
  }
  return transactions;
}

function structuredChoiceRequests(turns: unknown): unknown[] {
  if (!Array.isArray(turns)) return [];
  const seen = new Set<string>();
  const requests: unknown[] = [];
  for (const turn of turns) {
    const trace = asRecord(asRecord(turn).structuredChoiceTrace);
    if (!Object.keys(trace).length) continue;
    const fingerprint = `${String(trace.sourceAssistantTurnId ?? "")}|${String(trace.requestChoiceId ?? "")}|${String(trace.executionId ?? "")}`;
    if (seen.has(fingerprint)) continue;
    seen.add(fingerprint);
    requests.push(trace);
  }
  return requests;
}

function choiceExecutions(turns: unknown): unknown[] {
  return structuredChoiceRequests(turns).map((value) => {
    const trace = asRecord(value);
    return {
      executionId: trace.executionId ?? null,
      executionStatus: trace.executionStatus ?? null,
      action: trace.action ?? trace.persistedChoiceAction ?? null,
      attempt: trace.attempt ?? null,
      sourceAssistantTurnId: trace.sourceAssistantTurnId ?? null,
      sourceUserTurnId: trace.sourceUserTurnId ?? null,
      requestChoiceId: trace.requestChoiceId ?? null,
      persistedChoiceId: trace.persistedChoiceId ?? null,
      resolvedChoiceId: trace.resolvedChoiceId ?? null,
      planningSelectionRootTurnId: trace.planningSelectionRootTurnId ?? null,
      rootPortfolioId: trace.rootPortfolioId ?? null,
      requestContractFingerprint: trace.requestContractFingerprint ?? null,
      expectedBaseVersionId: trace.expectedBaseVersionId ?? null,
      resultVersionId: trace.resultVersionId ?? null,
      continuation: trace.continuation ?? null,
      checkpointFingerprint: trace.checkpointFingerprint ?? null,
      outcome: trace.outcome ?? null,
      versionDelta: trace.versionDelta ?? asRecord(trace.outcome).versionDelta ?? null,
      patchDelta: trace.patchDelta ?? asRecord(trace.outcome).patchDelta ?? null,
      routeWriteDelta: trace.routeWriteDelta ?? asRecord(trace.outcome).routeWriteDelta ?? null
    };
  });
}

function portfolioVisibility(snapshot: TripTestBundleSnapshot): Record<string, unknown> {
  const comparison = asRecord(snapshot.comparisonState);
  const plans = Array.isArray(comparison.plans) ? comparison.plans.map(asRecord) : [];
  const directionIds = new Set<string>();
  let declaredDirectionCount = 0;
  const turns = Array.isArray(snapshot.turns) ? snapshot.turns : [];
  for (const turn of turns) {
    const record = asRecord(turn);
    for (const key of ["planningSteps", "toolEvents"]) {
      for (const rawEvent of Array.isArray(record[key]) ? record[key] : []) {
        const metadata = asRecord(asRecord(rawEvent).metadata);
        for (const countKey of ["planningDirectionCount", "creativeBriefCount", "briefCount"]) {
          const value = metadata[countKey];
          if (typeof value === "number" && Number.isFinite(value)) {
            declaredDirectionCount = Math.max(declaredDirectionCount, value);
          }
        }
        for (const listKey of ["creativeBriefs", "planningDirections", "briefs"]) {
          for (const rawDirection of Array.isArray(metadata[listKey]) ? metadata[listKey] : []) {
            const direction = asRecord(rawDirection);
            const identity = String(direction.briefId ?? direction.id ?? direction.title ?? "").trim();
            if (identity) directionIds.add(identity);
          }
        }
      }
    }
  }
  const visiblePlans = plans.map((plan) => ({
    proposalId: plan.proposalId ?? null,
    rootPortfolioId: plan.rootPortfolioId ?? null,
    planningSelectionRootTurnId: plan.planningSelectionRootTurnId ?? null,
    status: plan.status ?? null,
    isPartial: plan.isPartial === true,
    isAdopted: plan.isAdopted === true,
    title: plan.title ?? null
  }));
  const verified = visiblePlans.filter((plan) => !plan.isPartial);
  return {
    planningDirectionCount: Math.max(declaredDirectionCount, directionIds.size),
    planningDirectionIds: [...directionIds],
    verifiedComparisonProposalCount: verified.length,
    verifiedComparisonProposalIds: verified.map((plan) => plan.proposalId),
    partialPlanCount: visiblePlans.length - verified.length,
    visibleCardCount: visiblePlans.length,
    focusedProposalId: comparison.focusedProposalId ?? null,
    adoptedProposalId: comparison.adoptedProposalId ?? null,
    planningSelectionRootTurnId: comparison.planningSelectionRootTurnId ?? null,
    rootPortfolioId: comparison.rootPortfolioId ?? null,
    plans: visiblePlans
  };
}

function utf8Size(value: string): number {
  return new TextEncoder().encode(value).byteLength;
}

function clipboardUtf8Size(value: string): number {
  return utf8Size(value.replace(/\r?\n/g, "\r\n"));
}

function truncateUtf8ForClipboard(value: string, maxBytes: number): string {
  let byteBudget = Math.min(maxBytes, utf8Size(value));
  while (byteBudget > 0) {
    const candidate = truncateUtf8(value, byteBudget);
    const clipboardBytes = clipboardUtf8Size(candidate);
    if (clipboardBytes <= maxBytes) return candidate;
    byteBudget -= Math.max(1, clipboardBytes - maxBytes);
  }
  return "";
}

function truncateUtf8(value: string, maxBytes: number): string {
  const encoded = new TextEncoder().encode(value);
  if (encoded.byteLength <= maxBytes) return value;
  const decoder = new TextDecoder("utf-8", { fatal: true });
  let end = Math.max(0, maxBytes);
  while (end > 0) {
    try {
      return decoder.decode(encoded.slice(0, end));
    } catch {
      end -= 1;
    }
  }
  return "";
}

function planningTrace(snapshot: TripTestBundleSnapshot): Record<string, unknown> {
  const turns = Array.isArray(snapshot.turns) ? snapshot.turns : [];
  const seen = new Set<string>();
  const events: Array<Record<string, unknown>> = [];
  const runs: Record<string, Array<Record<string, unknown>>> = {};
  for (const turn of turns) {
    if (!turn || typeof turn !== "object") continue;
    const record = turn as Record<string, unknown>;
    for (const key of ["planningSteps", "toolEvents"]) {
      const values = Array.isArray(record[key]) ? record[key] : [];
      for (const value of values) {
        if (!value || typeof value !== "object") continue;
        const event = value as Record<string, unknown>;
        const fingerprint = [event.id, event.type, event.timestamp, event.label].map((item) => String(item ?? "")).join("|");
        if (seen.has(fingerprint)) continue;
        seen.add(fingerprint);
        const copySafeEvent = stripTraceOnlyEvidence(event) as Record<string, unknown>;
        events.push(copySafeEvent);
        const metadata = asRecord(event.metadata);
        const preview = asRecord(metadata.resultPreview);
        const turnId = event.turnId ?? record.id ?? "unknown_turn";
        const runId = metadata.runId ?? preview.runId ?? record.planningRunId ?? `turn_${String(turnId)}_unlinked_run`;
        const cycleIndex = metadata.cycleIndex ?? preview.cycleIndex ?? 0;
        const runKey = `turn_${String(turnId)}__run_${String(runId)}__cycle_${String(cycleIndex)}`;
        (runs[runKey] ??= []).push(copySafeEvent);
      }
    }
  }
  return {
    runs,
    eventCount: events.length,
    pendingPlanningStep: snapshot.pendingPlanningStep,
    planningProcessSummary: stripTraceOnlyEvidence(snapshot.planningProcess)
  };
}

function stripTraceOnlyEvidence(value: unknown): unknown {
  if (Array.isArray(value)) return value.map((item) => stripTraceOnlyEvidence(item));
  if (!value || typeof value !== "object") return value;
  return Object.fromEntries(
    Object.entries(value as Record<string, unknown>)
      .filter(([key]) => {
        const normalized = key.replace(/[^a-z0-9]/gi, "").toLowerCase();
        return !["webdiscoveryattempts", "discoveryevidence", "discoveryprovenance"].includes(normalized);
      })
      .map(([key, item]) => [key, stripTraceOnlyEvidence(item)])
  );
}

function controllerPerformance(turns: unknown): unknown[] {
  if (!Array.isArray(turns)) return [];
  const evidence: unknown[] = [];
  const seen = new Set<string>();
  for (const turn of turns) {
    const record = asRecord(turn);
    for (const key of ["planningSteps", "toolEvents"]) {
      const events = Array.isArray(record[key]) ? record[key] : [];
      for (const item of events) {
        const event = asRecord(item);
        const metadata = asRecord(event.metadata);
        const samples = Array.isArray(metadata.controllerPerformance) ? metadata.controllerPerformance : [];
        if (!samples.length) continue;
        const fingerprint = JSON.stringify([
          record.id ?? null,
          event.sequence ?? null,
          event.type ?? null,
          event.timestamp ?? null,
          event.label ?? null,
          metadata.cycleIndex ?? null,
          metadata.decisionPath ?? null,
          samples
        ]);
        if (seen.has(fingerprint)) continue;
        seen.add(fingerprint);
        evidence.push({
          turnId: record.id ?? null,
          eventId: event.id ?? null,
          decisionPath: metadata.decisionPath ?? null,
          controllerFailureClass: metadata.controllerFailureClass ?? null,
          samples
        });
      }
    }
  }
  return evidence;
}
function conversationWithoutTrace(turns: unknown): unknown {
  if (!Array.isArray(turns)) return turns;
  return turns.map((turn) => {
    const record = asRecord(turn);
    const { planningSteps: _planningSteps, toolEvents: _toolEvents, ...conversation } = record;
    return conversation;
  });
}

function serialize(value: unknown): string {
  const safe = redact(value);
  return typeof safe === "string" ? safe : JSON.stringify(safe, null, 2);
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function redact(value: unknown, key = ""): unknown {
  if (isSensitiveKey(key)) return "[REDACTED]";
  if (Array.isArray(value)) return value.map((item) => redact(item));
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value as Record<string, unknown>).map(([childKey, child]) => [childKey, redact(child, childKey)]));
  }
  if (typeof value !== "string") return value;
  return value
    .replace(/Bearer\s+[A-Za-z0-9._~+\/-]+=*/gi, "Bearer [REDACTED]")
    .replace(/([?&](?:key|token|signature|auth|secret)=)[^&#\s]+/gi, "$1[REDACTED]")
    .replace(/(?:[A-Za-z]:\\(?:[^\s"'\\/]+\\)*[^\s"'\\/]+|\/(?:tmp|home|Users|var|workspace|mnt|private|opt|root|Volumes)(?:\/[^\s"'\\/]+)+)/g, "[LOCAL_PATH]");
}

function isSensitiveKey(key: string): boolean {
  const normalized = key.replace(/[^a-z0-9]/gi, "").toLowerCase();
  if (!normalized) return false;
  if (/^(prompt|completion|total)tokens?$/.test(normalized) || normalized === "tokenusage") return false;
  if (normalized === "reasoningcontentlength") return false;
  if (["reasoning", "reasoningtext", "reasoningcontent", "chainofthought"].includes(normalized)) return true;
  return /(?:authorization|cookie|password|secret|credential|privatekey|securityjscode|manualvalue|reasoningcontent)$/.test(normalized)
    || /(?:apikey|accesstoken|refreshtoken|idtoken|authtoken|bearertoken|sessiontoken|apitoken|^token)$/.test(normalized);
}

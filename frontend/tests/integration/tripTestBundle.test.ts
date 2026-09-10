import { describe, expect, test } from "vitest";
import { buildTripTestBundle, deliverTripDebugBundle } from "../../src/debug/tripTestBundle";

describe("trip test bundle", () => {
  test("emits one ordered versioned bundle and redacts secrets, query credentials, and local paths", () => {
    const text = buildTripTestBundle({
      capturedAt: "2026-07-12T00:00:00.000Z",
      session: {
        sessionId: "sess_1",
        apiKey: "secret-value",
        refreshToken: "refresh-leak",
        idToken: "identity-leak",
        clientSecret: "client-leak",
        securityJsCode: "amap-security-leak"
      },
      activeVersionId: "ver_1",
      turns: [
        {
          role: "user",
          content: "hello",
          structuredChoiceTrace: {
            sourceAssistantTurnId: "turn_source",
            requestChoiceId: "fallback:confirm_rule_safe_draft:choice_decision",
            persistedChoiceId: "fallback:confirm_rule_safe_draft:choice_decision",
            persistedChoiceAction: "confirm_rule_safe_draft",
            executionChoiceId: "fallback:confirm_rule_safe_draft:choice_decision",
            executionAction: "confirm_rule_safe_draft",
            executionId: "choice_exec_1",
            executionStatus: "succeeded",
            resultVersionId: "ver_1",
            planningSelectionRootTurnId: "turn_root",
            rootPortfolioId: "portfolio_1",
            requestContractFingerprint: "fp_1",
            outcome: { proposalDelta: 1, versionDelta: 0, patchDelta: 0, routeWriteDelta: 0 },
            outboundRequest: { manualValue: "private note" }
          }
        },
        {
          id: "turn_a",
          role: "assistant",
          planningRunId: "run_a",
          planningSteps: [
            {
              id: "decision_1",
              type: "agent_decision",
              label: "自治决策",
              timestamp: "2026-07-12T00:00:00Z",
              metadata: {
                cycleIndex: 0,
                creativeBriefCount: 3,
                reasoning_content: "hidden",
                decisionPath: "full",
                controllerFailureClass: "provider_timeout",
                controllerPerformance: [
                  {
                    callKind: "full",
                    payloadBytes: 1234,
                    httpStatus: 200,
                    responseBytes: 4321,
                    finishReason: "stop",
                    contentLength: 456,
                    reasoningContentLength: 789,
                    tokenUsage: { prompt_tokens: 100, completion_tokens: 200, total_tokens: 300 },
                    contextCharCounts: { observation: 456 },
                    workerQueueMs: 7,
                    connectDurationMs: null,
                    connectTimingAvailable: false,
                    preHeaderWaitDurationMs: 2500,
                    ttfbDurationMs: null,
                    readDurationMs: null,
                    currentReadElapsedMs: 1750,
                    responseHeadersReceived: true,
                    promptCacheSupported: false,
                    promptCacheHit: null
                  }
                ]
              }
            }
          ],
          toolEvents: [
            {
              id: "decision_1",
              type: "agent_decision",
              label: "自治决策",
              timestamp: "2026-07-12T00:00:00Z",
              metadata: { cycleIndex: 0 }
            }
          ]
        },
        {
          id: "turn_b",
          role: "assistant",
          planningRunId: "run_b",
          timelineMutationOutcome: { mutationId: "mutation_1", status: "success", postconditionPassed: true },
          timelineMutationTransaction: {
            mutationId: "mutation_1",
            sourceTurnId: "turn_user",
            intent: { operation: "replace_poi" },
            status: "success",
            baseVersionId: "ver_0",
            resultVersionId: "ver_1",
            patchId: "patch_1",
            binding: { targetSegmentIds: ["seg_1"] },
            resolvedReplacement: { selectedPoi: { id: "B0POI" } },
            compiledOperations: [{ op: "replace_segment_poi", segmentId: "seg_1" }],
            routeRefreshScope: { policy: "touched_pairs_only", pairs: [["seg_0", "seg_1"]] },
            beforeAfterDiff: { directChangedSegmentIds: ["seg_1"] },
            structuralVerifier: { passed: true },
            postconditionVerifier: { passed: true },
            rollbackPerformed: false
          },
          planningSteps: [
            {
              id: "decision_2",
              type: "agent_decision",
              label: "自治决策",
              timestamp: "2026-07-12T00:01:00Z",
              metadata: { cycleIndex: 0 }
            }
          ]
        }
      ],
      planningProcess: { processId: "proc_1", url: "https://example.com/a?token=secret-value" },
      pendingPlanningStep: null,
      itinerary: { scheduleDiagnostics: { overlapCount: 0 } },
      timelineText: "timeline C:\\Users\\me\\private\\trace.json C:\\tmp\\a /tmp/a https://example.com/a/b",
      sessionCaptureSource: "server_refresh",
      comparisonState: {
        planningSelectionRootTurnId: "turn_root",
        rootPortfolioId: "portfolio_1",
        focusedProposalId: "proposal_1",
        plans: [
          {
            proposalId: "proposal_1",
            rootPortfolioId: "portfolio_1",
            planningSelectionRootTurnId: "turn_root",
            status: "complete",
            isPartial: false,
            isAdopted: false,
            title: "高校夜景"
          }
        ]
      },
      mapInteraction: {
        initialized: true,
        mode: "plan_overview_preview",
        capabilities: { navigate: true, inspect: true, search: false, mutateItinerary: false },
        center: [116.4, 39.9],
        zoom: 13,
        dragCount: 1,
        zoomCount: 1,
        moveCount: 4,
        recentEvents: [{ type: "dragstart" }],
        pointerTarget: "div.amap-base"
      }
    });

    expect(text.startsWith("TRIP_DEBUG_BUNDLE_VERSION=4")).toBe(true);
    expect(text.endsWith("END_TRIP_DEBUG_BUNDLE")).toBe(true);
    for (const section of [
      "META",
      "COMPLETENESS",
      "CONVERSATION_TURNS",
      "EVENT_STORE",
      "TURN_EVENT_REFERENCES",
      "PLANNING_TRACE",
      "EXPLORATION_FRONTIERS",
      "PROPOSALS",
      "PROPOSAL_VERIFIERS",
      "ROUTE_EVIDENCE",
      "FRONTEND_UI_STATE"
    ]) {
      expect(text).toContain(`=== ${section} ===`);
    }
    expect(text).not.toContain("secret-value");
    expect(text).not.toContain("refresh-leak");
    expect(text).not.toContain("identity-leak");
    expect(text).not.toContain("client-leak");
    expect(text).not.toContain("amap-security-leak");
    expect(text).not.toContain("C:\\Users\\me");
    expect(text).not.toContain("C:\\tmp\\a");
    expect(text).not.toContain("/tmp/a");
    expect(text).toContain("https://example.com/a/b");
    expect(text).toContain("processId");
    expect(text).toContain('"sessionId": "sess_1"');
    expect(text).toContain('"decision_1"');
    expect(text).toContain('"decision_2"');
    expect(text).toContain("choice_exec_1");
    expect(text).toContain("confirm_rule_safe_draft");
    expect(text).toContain("mutation_1");
    expect(text).toContain('"payloadBytes": 1234');
    expect(text).toContain('"httpStatus": 200');
    expect(text).toContain('"responseBytes": 4321');
    expect(text).toContain('"reasoningContentLength": 789');
    expect(text).toContain('"total_tokens": 300');
    expect(text).toContain('"truncated": false');
    expect(text).toContain('"captureSource": "local_fallback"');
    expect(text).toContain('"dragCount": 1');
    expect(text).toContain('"serverRefresh"');
    expect(text).toContain('"currentReadElapsedMs": 1750');
    expect(text).toContain('"promptCacheSupported": false');
    expect(text).toContain('"workerQueueMs": 7');
    expect(text).toContain('"responseHeadersReceived": true');
    for (const field of [
      "resolvedReplacement",
      "compiledOperations",
      "routeRefreshScope",
      "beforeAfterDiff",
      "structuralVerifier",
      "postconditionVerifier"
    ]) {
      expect(text).toContain(`"${field}"`);
    }
    expect(text).not.toContain("private note");
    expect(text).not.toContain("hidden");
    expect(text).toContain("EVENT_STORE");
    const eventStore = text.split("=== EVENT_STORE ===")[1].split("=== TURN_EVENT_REFERENCES ===")[0];
    expect(eventStore.match(/decision_1/g)).toHaveLength(2);
  });

  test("keeps trace-only Web discovery evidence out of the default 500 KB copy bundle", () => {
    const text = buildTripTestBundle({
      capturedAt: "2026-07-30T00:00:00.000Z",
      session: {
        id: "session-trace-only",
        nested: {
          web_discovery_attempts: [{ selectedCandidates: [{ amapId: "B0TRACESESSION", name: "北京大学" }] }]
        }
      },
      activeVersionId: null,
      turns: [
        {
          id: "turn-trace-only",
          role: "assistant",
          diagnostics: {
            Discovery_Evidence: [{ seedName: "故宫", snippet: "B0TRACETURN" }]
          },
          planningSteps: [
            {
              id: "web-discovery",
              type: "portfolio_candidate_discovery",
              metadata: {
                resultPreview: {
                  webDiscoveryAttempts: [
                    {
                      query: "北京 高校 官方 地点",
                      seedGroundings: [
                        {
                          seedName: "清华大学",
                          selectedCandidates: [{ amapId: "B0TRACEONLY", name: "清华大学" }]
                        }
                      ]
                    }
                  ]
                }
              }
            }
          ]
        }
      ],
      planningProcess: {
        currentStage: "discovering",
        webDiscoveryAttempts: [
          {
            query: "北京 夜景 官方 地点",
            selectedCandidates: [{ amapId: "B0TRACEPROCESS", name: "花溪谷" }]
          }
        ]
      },
      pendingPlanningStep: null,
      itinerary: {
        metadata: {
          discoveryProvenance: {
            webUrl: "https://example.invalid/private",
            amapId: "B0TRACEITINERARY"
          }
        }
      },
      timelineText: ""
    });

    expect(text).toContain("webDiscoveryAttempts");
    expect(text).toContain("B0TRACEONLY");
    expect(text).toContain("B0TRACEPROCESS");
    expect(text).toContain("B0TRACESESSION");
    expect(text).toContain("B0TRACETURN");
    expect(text).toContain("B0TRACEITINERARY");
    expect(text).toContain("example.invalid/private");
    expect(text).toContain('"truncated": false');
  });

  test("caps a typical debug export at 500 KB and reports truncation", () => {
    const bundle = buildTripTestBundle({
      capturedAt: "2026-07-13T00:00:00.000Z",
      session: { id: "session-large" },
      activeVersionId: "version-large",
      turns: [{ id: "turn-large", role: "assistant", content: "大".repeat(600_000) }],
      planningProcess: null,
      pendingPlanningStep: null,
      itinerary: { notes: "大".repeat(600_000) },
      timelineText: "大".repeat(600_000)
    });

    expect(new TextEncoder().encode(bundle).byteLength).toBeGreaterThan(1_000_000);
    expect(bundle).toContain("TRIP_DEBUG_BUNDLE_VERSION=4");
    expect(bundle).not.toContain("TRUNCATED_TO_FIT_500KB_BUNDLE_LIMIT");
    expect(bundle).toContain("END_TRIP_DEBUG_BUNDLE");
  });

  test("keeps the hard byte cap when truncation lands inside a UTF-8 character", () => {
    for (const pad of [1, 2]) {
      const bundle = buildTripTestBundle({
        capturedAt: "2026-07-13T00:00:00.000Z",
        session: { id: "session-utf8" },
        activeVersionId: null,
        turns: [{ id: "turn-utf8", role: "assistant", content: "x".repeat(pad) + "中".repeat(600_000) }],
        planningProcess: null,
        pendingPlanningStep: null,
        itinerary: null,
        timelineText: ""
      });
      expect(new TextEncoder().encode(bundle).byteLength).toBeGreaterThan(500 * 1024);
      expect(bundle).not.toContain("�");
      expect(bundle).toContain("END_TRIP_DEBUG_BUNDLE");
    }
  });

  test("stays within 500 KB after Windows clipboard expands LF to CRLF", () => {
    const bundle = buildTripTestBundle({
      capturedAt: "2026-07-26T00:00:00.000Z",
      session: {
        sessionId: "session-crlf",
        text: Array.from({ length: 40_000 }, (_, index) => `诊断行-${index}`).join("\n")
      },
      activeVersionId: null,
      turns: [
        {
          id: "turn-large-trace",
          role: "assistant",
          structuredChoiceTrace: {
            sourceAssistantTurnId: "turn-source-large",
            requestChoiceId: "choice-large",
            persistedChoiceId: "choice-large",
            persistedChoiceAction: "retry_model_planning",
            executionId: "choice-exec-large",
            executionStatus: "succeeded",
            planningSelectionRootTurnId: "turn-root-large",
            rootPortfolioId: "portfolio-large",
            requestContractFingerprint: "fingerprint-large",
            outcome: {
              succeeded: true,
              proposalDelta: 1,
              blob: "choice-outcome".repeat(80_000),
              proposalIdsBefore: Array.from({ length: 500 }, (_, index) => `before-${index}-${"x".repeat(600)}`),
              proposalIdsAfter: Array.from({ length: 500 }, (_, index) => `after-${index}-${"y".repeat(600)}`),
              addedProposalIds: Array.from({ length: 500 }, (_, index) => `added-${index}-${"z".repeat(600)}`)
            },
            versionDelta: 0,
            patchDelta: 0,
            routeWriteDelta: 0
          },
          planningSteps: Array.from({ length: 1_000 }, (_, index) => ({
            id: `event-${index}`,
            type: "planning_step",
            label: `规划步骤-${index}`,
            timestamp: "2026-07-26T00:00:00.000Z",
            metadata: {
              runId: "run-large",
              cycleIndex: 0,
              payload: "轨迹".repeat(500),
              reasoning: "reasoning-leak",
              reasoningText: "reasoning-text-leak",
              chainOfThought: "chain-leak"
            }
          }))
        }
      ],
      planningProcess: null,
      pendingPlanningStep: null,
      itinerary: null,
      timelineText: Array.from({ length: 40_000 }, (_, index) => `时间轴-${index}`).join("\n")
    });

    const windowsClipboardBytes = new TextEncoder().encode(bundle.replace(/\r?\n/g, "\r\n")).byteLength;
    expect(windowsClipboardBytes).toBeGreaterThan(500 * 1024);
    expect(bundle).not.toContain("[TRUNCATED_TO_FIT_500KB_BUNDLE_LIMIT]");
    expect(bundle).toContain('"executionId": "choice-exec-large"');
    expect(bundle).not.toContain("reasoning-leak");
    expect(bundle).not.toContain("reasoning-text-leak");
    expect(bundle).not.toContain("chain-leak");
    for (const section of [
      "STRUCTURED_CHOICES",
      "CHOICE_EXECUTIONS",
      "CONTROLLER_RUNS",
      "EXPLORATION_FRONTIERS",
      "COMPARISON_STATE",
      "MAP_STATE"
    ]) {
      expect(bundle).toContain(`=== ${section} ===`);
    }
    expect(bundle).toContain("END_TRIP_DEBUG_BUNDLE");
  });

  test("copies every controller cycle when API events have no id", () => {
    const first = {
      sequence: 1,
      type: "agent_decision",
      timestamp: "2026-07-25T00:00:00Z",
      label: "自治决策",
      metadata: {
        cycleIndex: 0,
        decisionPath: "full",
        controllerPerformance: [{ callKind: "full", payloadBytes: 111 }]
      }
    };
    const second = {
      sequence: 2,
      type: "agent_decision",
      timestamp: "2026-07-25T00:00:01Z",
      label: "自治决策",
      metadata: {
        cycleIndex: 1,
        decisionPath: "repair",
        controllerPerformance: [{ callKind: "repair", payloadBytes: 222 }]
      }
    };
    const text = buildTripTestBundle({
      capturedAt: "2026-07-25T00:00:02Z",
      session: { id: "session-cycles" },
      activeVersionId: null,
      turns: [
        {
          id: "turn-cycles",
          role: "assistant",
          planningSteps: [first, second],
          toolEvents: [first]
        }
      ],
      planningProcess: null,
      pendingPlanningStep: null,
      itinerary: null,
      timelineText: ""
    });
    const section = text.split("=== EVENT_STORE ===")[1].split("=== TURN_EVENT_REFERENCES ===")[0];

    expect(section.match(/"payloadBytes": 111/g)).toHaveLength(1);
    expect(section.match(/"payloadBytes": 222/g)).toHaveLength(1);
  });

  test("downloads the complete JSON and copies only an index when clipboard fails", async () => {
    const copied: string[] = [];
    const downloads: Array<{ name: string; value: string }> = [];
    let attempts = 0;
    const result = await deliverTripDebugBundle({
      text: "完整文本".repeat(500_000),
      json: JSON.stringify({ full: "完整 JSON".repeat(500_000) }),
      filename: "trip-agent-debug-session-root.json",
      copy: async (value) => {
        attempts += 1;
        if (attempts === 1) throw new Error("clipboard_full");
        copied.push(value);
      },
      download: (name, value) => downloads.push({ name, value })
    });

    expect(result).toBe("downloaded");
    expect(downloads).toHaveLength(1);
    expect(downloads[0].value).toContain("完整 JSON");
    expect(copied[0]).toContain("剪贴板仅包含此索引");
  });
});
test("redacts alternate URL credential names and non-user absolute paths", () => {
  const text = buildTripTestBundle({
    capturedAt: "2026-08-01T00:00:00.000Z",
    session: {
      id: "session-redaction",
      diagnostics: "https://x.test/?api_key=TOPSECRET&access_token=LEAK&sig=SIGNATURE#token=FRAGMENT",
      canonical_signature: "STRUCTURAL_HASH",
      routeNarrative: "公交/地铁/换乘/步行"
    },
    activeVersionId: null,
    turns: [],
    planningProcess: null,
    pendingPlanningStep: null,
    itinerary: null,
    timelineText:
      "E:\\items\\trip\\trace.json C:\\tmp\\trace.json E:/items/trip/trace.json C:/tmp/trace.json /var/log/trip.log /workspace/project/trace.json /root/.cache/trace.json /data/trip/trace.json \\\\server\\share\\trace.json //server/share/trace.json"
  });

  for (const leaked of [
    "TOPSECRET",
    "LEAK",
    "SIGNATURE",
    "FRAGMENT",
    "E:\\items",
    "C:\\tmp",
    "E:/items",
    "C:/tmp",
    "/var/log",
    "/workspace/",
    "/root/",
    "/data/",
    "\\\\server\\share",
    "//server/share"
  ]) {
    expect(text).not.toContain(leaked);
  }
  expect(text).toContain("STRUCTURAL_HASH");
  expect(text).toContain("公交/地铁/换乘/步行");
  expect(text).toContain("[LOCAL_PATH]");
});

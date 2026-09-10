import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { AppShell } from "../../src/components/AppShell";
import type {
  AgentChoiceOption,
  AgentMessageResponse,
  AgentSession,
  ConversationTurn,
  ItineraryPlan
} from "../../src/services/apiClient";
import {
  createComparisonPreviewState,
  stablePlanColorKey,
  type ComparisonPlanProjection
} from "../../src/state/planComparisonPreview";
import { plannerStore } from "../../src/state/plannerStore";

const CREATED_AT = "2026-08-09T09:00:00Z";
const CHECKPOINT_ID = "checkpoint-night-journey";
const ROOT_TURN_ID = "turn_user_initial";
const ROOT_PORTFOLIO_ID = "portfolio_journey";
const USER_REQUEST =
  "今年国庆参观北京高校两日游，晚上看北京夜景。10月1日到2日，2天，中等预算，1人，公交地铁优先。每天午餐想体验当地特色美食。";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  window.localStorage.removeItem("trip.activeAgentSessionId");
  resetPlannerStore();
});

test("real DOM journey keeps one checkpoint, appends A/B, adopts B once, and restores it after reload", async () => {
  window.localStorage.removeItem("trip.activeAgentSessionId");
  resetPlannerStore();

  const firstChoices: AgentChoiceOption[] = [
    {
      id: "clarify_frequency_every_night",
      index: 1,
      kind: "clarification_checkpoint",
      action: "continue_clarification",
      label: "每个可用夜晚安排不同夜景",
      semanticValue: "every_available_evening_distinct",
      dimensionId: "night_view_frequency",
      checkpointId: CHECKPOINT_ID
    },
    {
      id: "clarify_frequency_once",
      index: 2,
      kind: "clarification_checkpoint",
      action: "continue_clarification",
      label: "两天只安排一次夜景",
      semanticValue: "one_evening_only",
      dimensionId: "night_view_frequency",
      checkpointId: CHECKPOINT_ID
    }
  ];
  const secondChoices: AgentChoiceOption[] = [
    {
      id: "clarify_style_public_outdoor",
      index: 1,
      kind: "clarification_checkpoint",
      action: "continue_clarification",
      label: "公共开放的户外夜景",
      semanticValue: "public_outdoor_night_view",
      dimensionId: "night_view_style",
      checkpointId: CHECKPOINT_ID
    },
    {
      id: "clarify_style_indoor",
      index: 2,
      kind: "clarification_checkpoint",
      action: "continue_clarification",
      label: "室内付费观景平台",
      semanticValue: "paid_indoor_observation",
      dimensionId: "night_view_style",
      checkpointId: CHECKPOINT_ID
    }
  ];

  const planA = comparisonProjection({
    proposalId: "proposal_a",
    sourceAssistantTurnId: "turn_assistant_a",
    choiceId: "choice_adopt_a",
    title: "学府书声映照京城夜色",
    placeSeed: "a"
  });
  const planB = comparisonProjection({
    proposalId: "proposal_b",
    sourceAssistantTurnId: "turn_assistant_b",
    choiceId: "choice_adopt_b",
    title: "校园清韵漫入公园华灯",
    placeSeed: "b"
  });
  const continueChoice: AgentChoiceOption = {
    id: "choice_continue_from_a",
    index: 2,
    kind: "portfolio_partial_more_plans",
    action: "retry_model_planning",
    label: "继续生成其他方案",
    lifecycle: "offered",
    planningSelectionRootTurnId: ROOT_TURN_ID,
    rootPortfolioId: ROOT_PORTFOLIO_ID
  };

  const roundOne = response(
    turn(ROOT_TURN_ID, "user", 1, USER_REQUEST),
    turn("turn_assistant_question_1", "assistant", 2, "两个夜晚都要安排夜景吗？", {
      choiceOptions: firstChoices,
      clarificationCheckpoint: {
        checkpointId: CHECKPOINT_ID,
        status: "active",
        answers: [],
        resolvedDimensions: [],
        unresolvedDimensions: ["night_view_frequency", "night_view_style"],
        experienceSpecs: [],
        candidateGapSummary: { nightViewSlots: 2 }
      }
    })
  );
  const roundTwo = response(
    turn("turn_user_frequency", "user", 3, firstChoices[0].label ?? "", {
      structuredChoiceTrace: {
        sourceAssistantTurnId: "turn_assistant_question_1",
        resolvedChoiceId: firstChoices[0].id,
        executionStatus: "succeeded"
      }
    }),
    turn("turn_assistant_question_2", "assistant", 4, "夜景更偏向哪一种公共体验？", {
      choiceOptions: secondChoices,
      clarificationCheckpoint: {
        checkpointId: CHECKPOINT_ID,
        status: "active",
        answers: [
          {
            dimensionId: "night_view_frequency",
            semanticValue: "every_available_evening_distinct",
            label: firstChoices[0].label
          }
        ],
        resolvedDimensions: ["night_view_frequency"],
        unresolvedDimensions: ["night_view_style"],
        experienceSpecs: [
          {
            intentType: "night_view",
            occurrenceCount: 2,
            distinctPhysicalPoiRequired: true
          }
        ],
        candidateGapSummary: { nightViewSlots: 2, groundedNightViewCount: 0 }
      }
    })
  );
  const planAResponse = response(
    turn("turn_user_style", "user", 5, secondChoices[0].label ?? "", {
      structuredChoiceTrace: {
        sourceAssistantTurnId: "turn_assistant_question_2",
        resolvedChoiceId: secondChoices[0].id,
        executionStatus: "succeeded"
      }
    }),
    turn("turn_assistant_a", "assistant", 6, "方案 A 已完成严格核验。", {
      comparisonProjectionUpdateMode: "replace",
      comparisonProjections: [planA],
      choiceOptions: [proposalChoice(planA), continueChoice],
      clarificationCheckpoint: {
        checkpointId: CHECKPOINT_ID,
        status: "resolved",
        answers: [
          {
            dimensionId: "night_view_frequency",
            semanticValue: "every_available_evening_distinct"
          },
          {
            dimensionId: "night_view_style",
            semanticValue: "public_outdoor_night_view"
          }
        ],
        resolvedDimensions: ["night_view_frequency", "night_view_style"],
        unresolvedDimensions: [],
        experienceSpecs: [
          {
            intentType: "night_view",
            occurrenceCount: 2,
            distinctPhysicalPoiRequired: true,
            experienceShape: "public_outdoor"
          }
        ],
        candidateGapSummary: { nightViewSlots: 2, groundedNightViewCount: 2 }
      }
    })
  );
  const planBResponse = response(
    turn("turn_user_continue", "user", 7, continueChoice.label ?? "", {
      structuredChoiceTrace: {
        sourceAssistantTurnId: "turn_assistant_a",
        resolvedChoiceId: continueChoice.id,
        executionStatus: "succeeded",
        outcome: { versionDelta: 0, patchDelta: 0, routeWriteDelta: 0 }
      }
    }),
    turn("turn_assistant_b", "assistant", 8, "方案 B 已追加，方案 A 保持可见。", {
      comparisonProjectionUpdateMode: "append",
      comparisonProjections: [planB],
      choiceOptions: [proposalChoice(planB)]
    })
  );
  const adoptedItinerary = itineraryFromProjection(planB);
  const adoptionResponse = response(
    turn("turn_user_adopt_b", "user", 9, "采用此方案", {
      itineraryVersionId: "version_b",
      structuredChoiceTrace: {
        sourceAssistantTurnId: "turn_assistant_b",
        resolvedChoiceId: planB.choiceId,
        executionStatus: "succeeded",
        resultVersionId: "version_b",
        outcome: {
          versionDelta: 1,
          patchDelta: 1,
          routeWriteDelta: planB.routeEvidence.length
        }
      }
    }),
    turn("turn_assistant_adopt_b", "assistant", 10, "已采用方案 B，并写入正式时间轴。"),
    adoptedItinerary,
    { id: "version_b", versionNumber: 1, sourceType: "agent_portfolio_commit" }
  );

  const roundTwoDeferred = deferred<Response>();
  const adoptionDeferred = deferred<Response>();
  const streamBodies: Array<Record<string, unknown>> = [];
  const persistedTurns: ConversationTurn[] = [];
  let persistedItinerary: ItineraryPlan | null = null;
  let persistedVersionId: string | null = null;
  let streamIndex = 0;
  let reloadMode = false;

  const persist = (message: AgentMessageResponse) => {
    persistedTurns.push(message.userTurn, message.assistantTurn);
    if (message.itinerary) persistedItinerary = message.itinerary;
    if (message.version?.id) persistedVersionId = message.version.id;
  };
  const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    const method = init?.method ?? "GET";
    if (path.endsWith("/providers/status")) {
      return jsonResponse({ mode: "mock", default: [], mock: [] });
    }
    if (path.endsWith("/preferences/memory")) {
      return jsonResponse({
        userId: "journey-user",
        memoryText: "# 我的旅行偏好",
        autoUpdateEnabled: true,
        createdAt: CREATED_AT,
        updatedAt: CREATED_AT
      });
    }
    if (path.endsWith("/preferences/extract")) {
      return jsonResponse({ summaryCard: preferenceCard() });
    }
    if (path.endsWith("/agent/sessions/current")) {
      return reloadMode
        ? jsonResponse(persistedSession(persistedTurns, persistedItinerary, persistedVersionId))
        : jsonResponse({ detail: "No active conversation session" }, 404);
    }
    if (reloadMode && path.endsWith("/agent/sessions/sess_journey") && method === "GET") {
      return jsonResponse(persistedSession(persistedTurns, persistedItinerary, persistedVersionId));
    }
    if (path.endsWith("/agent/sessions") && method === "POST") {
      return jsonResponse(emptySession());
    }
    if (path.endsWith("/agent/sessions/sess_journey/messages/stream") && method === "POST") {
      streamBodies.push(JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>);
      const index = streamIndex++;
      if (index === 1) return roundTwoDeferred.promise;
      if (index === 4) return adoptionDeferred.promise;
      const message = [roundOne, roundTwo, planAResponse, planBResponse, adoptionResponse][index];
      persist(message);
      return streamResponse(message);
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  let mounted = render(<AppShell />);
  const input = await screen.findByLabelText("Agent 对话文本");
  fireEvent.change(input, { target: { value: USER_REQUEST } });
  fireEvent.keyDown(input, { key: "Enter" });

  const firstTarget = optionBySemantic(firstChoices, "every_available_evening_distinct");
  await waitFor(() => expect(choiceButton(firstTarget)).not.toBeNull());
  const firstProgress = screen.getByLabelText("澄清进度");
  expect(firstProgress.textContent).toContain("已确认 0/2 项关键约束");
  expect(firstProgress.textContent).toContain("当前只需回答：两个夜晚都要安排夜景吗？");
  expect(firstProgress.textContent).toContain("还需补齐 2 个规划目标");
  expect(firstProgress.textContent).not.toContain("night_view_frequency");
  fireEvent.click(choiceButton(firstTarget));

  await screen.findByRole("button", { name: /正在按新约束继续/ });
  await waitFor(() => expect(streamBodies).toHaveLength(2));
  expect(selectedChoice(streamBodies[1])).toEqual({
    sourceAssistantTurnId: "turn_assistant_question_1",
    choiceId: firstTarget.id
  });
  expect(plannerStore.getSnapshot().activeVersionId).toBeNull();

  persist(roundTwo);
  roundTwoDeferred.resolve(streamResponse(roundTwo));
  const secondTarget = optionBySemantic(secondChoices, "public_outdoor_night_view");
  await waitFor(() => expect(choiceButton(secondTarget)).not.toBeNull());
  await waitFor(() => expect(choiceButton(secondTarget).disabled).toBe(false));
  expect(choiceButton(secondTarget).dataset.checkpointId).toBe(CHECKPOINT_ID);
  const secondQuestion = plannerStore
    .getSnapshot()
    .conversationTurns.find((item) => item.id === "turn_assistant_question_2");
  expect(secondQuestion?.clarificationCheckpoint).toMatchObject({
    checkpointId: CHECKPOINT_ID,
    resolvedDimensions: ["night_view_frequency"],
    unresolvedDimensions: ["night_view_style"]
  });
  const secondProgress = screen.getByLabelText("澄清进度");
  expect(secondProgress.textContent).toContain("已确认 1/2 项关键约束");
  expect(secondProgress.textContent).toContain("已形成 1 项可执行体验约束");
  expect(secondProgress.textContent).toContain("当前只需回答：夜景更偏向哪一种公共体验？");
  expect(secondProgress.textContent).toContain("还需补齐 2 个规划目标");
  expect(secondProgress.textContent).not.toContain("night_view_style");
  expect(plannerStore.getSnapshot().activeVersionId).toBeNull();

  reloadMode = true;
  mounted.unmount();
  resetPlannerStore();
  mounted = render(<AppShell />);

  const restoredProgress = await screen.findByLabelText("澄清进度");
  expect(restoredProgress.textContent).toContain("已确认 1/2 项关键约束");
  await waitFor(() => expect(choiceButton(secondTarget)).not.toBeNull());
  expect(choiceButton(secondTarget).dataset.checkpointId).toBe(CHECKPOINT_ID);
  expect(document.querySelector(`[data-semantic-value="${String(firstTarget.semanticValue)}"]`)).toBeNull();
  expect(plannerStore.getSnapshot().activeVersionId).toBeNull();

  fireEvent.click(choiceButton(secondTarget));
  await waitFor(() => expect(streamBodies).toHaveLength(3));
  await waitFor(() => expect(plannerStore.getSnapshot().comparisonPreview.plans).toHaveLength(1));
  expect(selectedChoice(streamBodies[2])).toEqual({
    sourceAssistantTurnId: "turn_assistant_question_2",
    choiceId: secondTarget.id
  });
  expect(plannerStore.getSnapshot().activeVersionId).toBeNull();
  expect(planA.days).toHaveLength(2);
  expect(planA.routeEvidence).toHaveLength(2);

  fireEvent.click(choiceButton(continueChoice));
  await waitFor(() => expect(plannerStore.getSnapshot().comparisonPreview.plans).toHaveLength(2));
  expect(plannerStore.getSnapshot().comparisonPreview.plans.map((item) => item.proposalId)).toEqual([
    "proposal_a",
    "proposal_b"
  ]);
  expect(plannerStore.getSnapshot().comparisonPreview.plans[0].title).toBe(planA.title);
  expect(plannerStore.getSnapshot().activeVersionId).toBeNull();
  expect(physicalPoiIds(planA)).not.toEqual(physicalPoiIds(planB));

  fireEvent.click(screen.getByRole("tab", { name: "行程对比" }));
  const planBCard = document.querySelector('[data-proposal-id="proposal_b"]') as HTMLElement;
  expect(planBCard).not.toBeNull();
  const adoptButton = within(planBCard).getByRole("button", { name: "采用此方案" });
  fireEvent.click(adoptButton);
  fireEvent.click(adoptButton);
  await waitFor(() => expect(streamBodies).toHaveLength(5));
  expect(streamBodies.filter((body) => selectedChoice(body)?.choiceId === planB.choiceId)).toHaveLength(1);
  expect(selectedChoice(streamBodies[4])).toEqual({
    sourceAssistantTurnId: "turn_assistant_b",
    choiceId: planB.choiceId
  });

  persist(adoptionResponse);
  adoptionDeferred.resolve(streamResponse(adoptionResponse));
  await waitFor(() => expect(plannerStore.getSnapshot().activeVersionId).toBe("version_b"));
  fireEvent.click(screen.getByRole("tab", { name: "行程对比" }));
  const adoptedButton = await screen.findByRole("button", { name: "已采用" });
  expect((adoptedButton as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(adoptedButton);
  expect(streamBodies).toHaveLength(5);
  expect(plannerStore.getSnapshot().comparisonPreview.adoptedProposalId).toBe("proposal_b");
  expect(plannerStore.getSnapshot().itineraryPlan?.id).toBe("plan_b");

  mounted.unmount();
  resetPlannerStore();
  render(<AppShell />);

  await waitFor(() => expect(plannerStore.getSnapshot().activeVersionId).toBe("version_b"));
  expect(plannerStore.getSnapshot().comparisonPreview.plans.map((item) => item.proposalId)).toEqual([
    "proposal_a",
    "proposal_b"
  ]);
  expect(plannerStore.getSnapshot().comparisonPreview.adoptedProposalId).toBe("proposal_b");
  expect(plannerStore.getSnapshot().itineraryPlan?.days).toHaveLength(2);
  fireEvent.click(screen.getByRole("tab", { name: "行程对比" }));
  expect(document.querySelector('[data-proposal-id="proposal_a"]')).not.toBeNull();
  const reloadedB = document.querySelector('[data-proposal-id="proposal_b"]') as HTMLElement;
  expect(reloadedB).not.toBeNull();
  expect((within(reloadedB).getByRole("button", { name: "已采用" }) as HTMLButtonElement).disabled).toBe(true);
});

test("free-text clarification click exposes a truthful pending state and keeps opaque identity", async () => {
  window.localStorage.removeItem("trip.activeAgentSessionId");
  resetPlannerStore();
  const manualChoice: AgentChoiceOption = {
    id: "clarification:checkpoint-free-text:manual",
    index: 3,
    kind: "custom_input",
    action: "continue_clarification",
    label: "我自己填写",
    checkpointId: "checkpoint-free-text",
    dimensionId: "night_view.experience_mode",
    allowsManualInput: true
  };
  const assistantQuestion = turn("turn_assistant_free_text", "assistant", 2, "还需要确认一种体验偏好。", {
    choiceOptions: [
      {
        id: "clarification:checkpoint-free-text:option-a",
        index: 1,
        kind: "clarification_checkpoint",
        action: "continue_clarification",
        label: "选项甲",
        semanticValue: { experienceMode: "mode_a" },
        checkpointId: "checkpoint-free-text",
        dimensionId: "night_view.experience_mode"
      },
      {
        id: "clarification:checkpoint-free-text:option-b",
        index: 2,
        kind: "clarification_checkpoint",
        action: "continue_clarification",
        label: "选项乙",
        semanticValue: { experienceMode: "mode_b" },
        checkpointId: "checkpoint-free-text",
        dimensionId: "night_view.experience_mode"
      },
      manualChoice
    ],
    clarificationCheckpoint: {
      schemaVersion: "clarification-checkpoint-v1",
      checkpointId: "checkpoint-free-text",
      planningRootId: ROOT_TURN_ID,
      contractVersion: 2,
      status: "awaiting_answer",
      resolvedAnswers: [
        {
          dimensionId: "night_view.frequency",
          semanticValue: { occurrencePolicy: "every_available_evening" },
          source: "structured_option"
        }
      ],
      ambiguities: [
        { dimensionId: "night_view.frequency", resolved: true },
        { dimensionId: "night_view.experience_mode", resolved: false }
      ],
      experienceSpecs: [{ intentType: "night_view", frequency: "every_available_evening" }],
      candidateGapSummary: { missingOccurrences: 2 },
      nextQuestionDimensionId: "night_view.experience_mode",
      question: {
        dimensionId: "night_view.experience_mode",
        question: "你愿意接受哪些公共空间体验？",
        whyItMatters: "这会改变候选准入与路线核验。",
        allowFreeText: true
      }
    }
  });
  const session: AgentSession = {
    ...emptySession(),
    turns: [turn(ROOT_TURN_ID, "user", 1, USER_REQUEST), assistantQuestion]
  };
  const completion = response(
    turn("turn_user_free_text", "user", 3, "偏好安静且无需预约的公共空间", {
      structuredChoiceTrace: {
        sourceAssistantTurnId: assistantQuestion.id,
        resolvedChoiceId: manualChoice.id,
        executionStatus: "succeeded"
      }
    }),
    turn("turn_assistant_free_text_received", "assistant", 4, "已收到补充，正在由 Agent 归一化语义。")
  );
  const pendingResponse = deferred<Response>();
  const requestBodies: Array<Record<string, unknown>> = [];
  const fetchMock = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url);
    if (path.endsWith("/providers/status")) return jsonResponse({ mode: "mock", default: [], mock: [] });
    if (path.endsWith("/preferences/memory")) {
      return jsonResponse({
        userId: "journey-user",
        memoryText: "# 我的旅行偏好",
        autoUpdateEnabled: true,
        createdAt: CREATED_AT,
        updatedAt: CREATED_AT
      });
    }
    if (path.endsWith("/preferences/extract")) return jsonResponse({ summaryCard: preferenceCard() });
    if (path.endsWith("/agent/sessions/current")) return jsonResponse(session);
    if (path.endsWith("/agent/sessions/sess_journey/messages/stream") && init?.method === "POST") {
      requestBodies.push(JSON.parse(String(init.body ?? "{}")) as Record<string, unknown>);
      return pendingResponse.promise;
    }
    return jsonResponse({}, 404);
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<AppShell />);

  const progress = await screen.findByLabelText("澄清进度");
  expect(progress.textContent).toContain("已确认 1/2 项关键约束");
  expect(progress.textContent).toContain("还需补齐 2 个规划目标");
  expect(progress.textContent).not.toContain("missingOccurrences");
  const manualInput = screen.getByPlaceholderText("补充你的偏好或限制");
  fireEvent.change(manualInput, {
    target: { value: "偏好安静且无需预约的公共空间" }
  });
  const manualButton = document.querySelector(`[data-choice-id="${manualChoice.id}"]`) as HTMLButtonElement;
  expect(manualButton).not.toBeNull();
  fireEvent.click(manualButton);

  await waitFor(() => expect(manualButton.textContent).toBe("正在按新约束继续"));
  const context = requestBodies[0]?.context as Record<string, unknown> | undefined;
  expect(context?.selectedAgentChoice).toEqual({
    sourceAssistantTurnId: assistantQuestion.id,
    choiceId: manualChoice.id,
    manualValue: "偏好安静且无需预约的公共空间"
  });
  expect(plannerStore.getSnapshot().activeVersionId).toBeNull();

  pendingResponse.resolve(streamResponse(completion));
  await screen.findByText("已收到补充，正在由 Agent 归一化语义。");
});

function comparisonProjection(input: {
  proposalId: string;
  sourceAssistantTurnId: string;
  choiceId: string;
  title: string;
  placeSeed: "a" | "b";
}): ComparisonPlanProjection {
  const places =
    input.placeSeed === "a"
      ? [
          ["清华大学", "B000A0001", 40.003, 116.326],
          ["中央电视塔", "B000A0002", 39.918, 116.3],
          ["北京大学", "B000A0003", 39.992, 116.305],
          ["奥林匹克塔", "B000A0004", 40.012, 116.393]
        ]
      : [
          ["中国人民大学", "B000B0001", 39.969, 116.321],
          ["景山公园", "B000B0002", 39.925, 116.397],
          ["北京师范大学", "B000B0003", 39.962, 116.366],
          ["奥林匹克森林公园", "B000B0004", 40.016, 116.389]
        ];
  const days = [1, 2].map((dayNumber) => {
    const first = places[(dayNumber - 1) * 2];
    const second = places[(dayNumber - 1) * 2 + 1];
    return {
      id: `${input.proposalId}_day_${dayNumber}`,
      dayNumber,
      title: `第 ${dayNumber} 天高校与公共夜景`,
      weatherSummary: "晴",
      riskSummary: "路线已核验",
      totalEstimatedCost: 80,
      pendingSlots: [],
      segments: [
        segment(input.proposalId, dayNumber, "campus", first, "09:00", "campus_visit"),
        segment(input.proposalId, dayNumber, "night", second, "19:00", "night_view")
      ]
    };
  }) as unknown as ItineraryPlan["days"];
  const routeEvidence = days.map((day) => {
    const [from, to] = day.segments;
    return {
      id: `${input.proposalId}_route_${day.dayNumber}`,
      fromSegmentId: from.id,
      toSegmentId: to.id,
      fromPoiId: from.poi.id,
      toPoiId: to.poi.id,
      provider: "amap-webservice",
      source: "amap-webservice",
      status: "success",
      mode: "transit",
      label: "公交地铁",
      isSelected: true,
      sortOrder: day.dayNumber,
      transportMode: "transit",
      distanceMeters: 6200 + day.dayNumber * 100,
      durationSeconds: 1800 + day.dayNumber * 60,
      durationMinutes: 31,
      costAmount: 5,
      costCurrency: "CNY",
      costEstimate: 5,
      crowdingRisk: "medium",
      polyline: [
        [from.poi.longitude ?? 0, from.poi.latitude ?? 0],
        [to.poi.longitude ?? 0, to.poi.latitude ?? 0]
      ],
      steps: [],
      providerPayload: { status: "1" },
      queriedAt: CREATED_AT
    };
  });
  return {
    planningSelectionRootTurnId: ROOT_TURN_ID,
    rootPortfolioId: ROOT_PORTFOLIO_ID,
    proposalId: input.proposalId,
    sourceAssistantTurnId: input.sourceAssistantTurnId,
    choiceId: input.choiceId,
    status: "complete",
    isPartial: false,
    isAdopted: false,
    adoptionReady: true,
    strictlyVerified: true,
    structureReady: true,
    adoptionMode: "complete",
    activeVersionId: null,
    expectedBaseVersionId: null,
    title: input.title,
    days,
    pendingSlots: [],
    routeEvidence,
    routeStatus: "route_ready",
    routeExpectedLegCount: 2,
    routeVerifiedLegCount: 2,
    routeErrorLegCount: 0,
    budgetSummary: "中等预算，证据完整",
    budgetTier: "medium",
    budgetTierLabel: "中等预算",
    budgetStatus: "verified",
    budgetEvidenceCount: 4,
    unknownCostSegmentCount: 0,
    routeSummary: "2/2 相邻路线已由高德核验",
    blockingReasons: [],
    comparisonRole: "candidate_proposal",
    originProjectionMode: "full_proposal",
    currentReadiness: "route_ready",
    promotionStatus: "not_promotable",
    nextAction: "adopt_proposal",
    nextActionLabel: "采用此方案",
    tradeoffSummary: input.placeSeed === "a" ? "经典高校与城市高点" : "社区高校与公共公园夜色",
    colorKey: stablePlanColorKey(input.proposalId)
  };
}

function segment(
  proposalId: string,
  dayNumber: number,
  role: string,
  place: Array<string | number>,
  startTime: string,
  intentType: string
) {
  const [name, amapId, latitude, longitude] = place;
  return {
    id: `${proposalId}_day_${dayNumber}_${role}`,
    startTime,
    endTime: role === "campus" ? "11:00" : "20:30",
    kind: "activity",
    poi: {
      id: `${proposalId}_${String(amapId)}`,
      amapId: String(amapId),
      name: String(name),
      city: "北京",
      category: role === "campus" ? "campus" : "night_view",
      type: role === "campus" ? "科教文化服务;学校;高等院校" : "风景名胜;观景点",
      district: "北京市",
      address: `${String(name)}地址`,
      latitude: Number(latitude),
      longitude: Number(longitude),
      source: "amap-place-search",
      sourceNote: "来源：高德地图",
      confidence: 0.98,
      groundingStatus: "verified_amap",
      routeable: true,
      mapReady: true
    },
    transportMode: "transit",
    estimatedCost: role === "campus" ? 0 : 20,
    notes: role === "campus" ? "高校参观" : "公共开放户外夜景",
    semanticMetadata: {
      routeAnchor: true,
      intentType,
      requirementLevel: "hard",
      groundingStatus: "selected"
    }
  };
}

function itineraryFromProjection(plan: ComparisonPlanProjection): ItineraryPlan {
  return {
    id: "plan_b",
    title: plan.title,
    city: "北京",
    templateType: "creative_portfolio",
    budgetTarget: 800,
    budgetTier: "medium",
    budgetEstimate: 320,
    budgetDeltaExplanation: "中等预算内",
    decisionRationale: plan.tradeoffSummary,
    status: "active",
    days: plan.days,
    routeOptions: plan.routeEvidence,
    weatherSignals: [],
    trafficCrowdingSignals: [],
    poiRiskAlerts: [],
    ticketLookupResults: [],
    routeWarnings: []
  };
}

function proposalChoice(plan: ComparisonPlanProjection): AgentChoiceOption {
  return {
    id: plan.choiceId,
    index: 1,
    kind: "plan_proposal",
    action: "select_plan_proposal",
    label: plan.title,
    lifecycle: "offered",
    comparisonProjection: plan,
    planningSelectionRootTurnId: plan.planningSelectionRootTurnId,
    rootPortfolioId: plan.rootPortfolioId
  };
}

function turn(
  id: string,
  role: ConversationTurn["role"],
  turnIndex: number,
  content: string,
  extra: Partial<ConversationTurn> = {}
): ConversationTurn {
  return {
    id,
    role,
    content,
    turnIndex,
    status: "active",
    choiceOptions: [],
    planningSteps: [],
    toolEvents: [],
    createdAt: CREATED_AT,
    updatedAt: CREATED_AT,
    ...extra
  };
}

function response(
  userTurn: ConversationTurn,
  assistantTurn: ConversationTurn,
  itinerary: ItineraryPlan | null = null,
  version: AgentMessageResponse["version"] = null
): AgentMessageResponse {
  return {
    userTurn,
    assistantTurn,
    itinerary,
    version,
    pendingPoiCandidates: [],
    warnings: [],
    planningSteps: [],
    toolEvents: [],
    executionMode: "bounded_agent",
    terminalStatus: itinerary ? "success" : "needs_confirmation",
    agentDecisionCount: 1,
    outcomeStatuses: {}
  };
}

function streamResponse(message: AgentMessageResponse): Response {
  return new Response(
    `${JSON.stringify({ event: "user_turn", data: message.userTurn })}\n${JSON.stringify({
      event: "message_response",
      data: message
    })}\n`,
    { status: 200, headers: { "Content-Type": "application/x-ndjson" } }
  );
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" }
  });
}

function preferenceCard() {
  return {
    id: "card_journey",
    profileId: "pref_journey",
    partySize: 1,
    travelerTypes: ["adult"],
    budgetRange: "中等预算",
    pacePreference: "标准",
    summaryText: "公交地铁优先",
    items: [{ label: "公交地铁优先", sourceText: "公交地铁优先" }],
    status: "draft"
  };
}

function emptySession(): AgentSession {
  return {
    sessionId: "sess_journey",
    status: "active",
    city: "北京",
    title: "北京高校与夜景",
    activePlanId: "plan_journey",
    activeVersionId: null,
    turns: [],
    itinerary: null,
    pendingPoiCandidates: []
  };
}

function persistedSession(
  turns: ConversationTurn[],
  itinerary: ItineraryPlan | null,
  activeVersionId: string | null
): AgentSession {
  return {
    ...emptySession(),
    activePlanId: itinerary?.id ?? "plan_journey",
    activeVersionId,
    turns: [...turns],
    itinerary
  };
}

function optionBySemantic(options: AgentChoiceOption[], semanticValue: string) {
  const option = options.find((item) => item.semanticValue === semanticValue);
  if (!option?.id) throw new Error(`missing semantic option: ${semanticValue}`);
  return option;
}

function choiceButton(option: AgentChoiceOption): HTMLButtonElement {
  const selector = option.semanticValue
    ? `[data-semantic-value="${String(option.semanticValue)}"]`
    : `[data-choice-id="${String(option.id)}"]`;
  const button = document.querySelector(selector);
  if (!(button instanceof HTMLButtonElement)) {
    throw new Error(`choice button not found: ${selector}`);
  }
  return button;
}

function selectedChoice(body: Record<string, unknown>) {
  const context = body.context as Record<string, unknown> | undefined;
  return context?.selectedAgentChoice as { sourceAssistantTurnId: string; choiceId: string } | undefined;
}

function physicalPoiIds(plan: ComparisonPlanProjection) {
  return plan.days.flatMap((day) => day.segments.map((item) => String(item.poi.amapId || "")));
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolver) => {
    resolve = resolver;
  });
  return { promise, resolve };
}

function resetPlannerStore() {
  plannerStore.setState({
    selectedCity: "北京",
    activePanel: "agent",
    providerMode: "mock",
    agentSession: null,
    agentSessions: [],
    conversationTurns: [],
    activeVersionId: null,
    itineraryPlan: null,
    planComparison: null,
    preferenceCard: null,
    preferenceMemory: null,
    pendingPoiCandidates: [],
    activeDensityMapComparison: null,
    poiSelectionStatuses: {},
    supersededTurnIds: [],
    lastPatchError: "",
    candidateMapPois: [],
    selectedMapPoi: null,
    selectedDayNumber: 1,
    selectedSegmentId: null,
    selectedRouteOptionId: null,
    previewRouteOptionId: null,
    routeWarnings: [],
    itineraryAgentContext: null,
    timelineCopyText: "",
    lastPlanningRun: null,
    comparisonPreview: createComparisonPreviewState()
  });
}

import { expect, test, type Route } from "@playwright/test";

const CREATED_AT = "2026-08-09T09:00:00Z";
const CHECKPOINT_ID = "checkpoint-browser-journey";
const ROOT_TURN_ID = "turn_browser_root";
const PORTFOLIO_ID = "portfolio_browser";
const SESSION_ID = "sess_browser";
const USER_REQUEST =
  "今年国庆参观北京高校两日游，晚上看北京夜景。10月1日到2日，2天，中等预算，1人，公交地铁优先。每天午餐想体验当地特色美食。";

test("deterministic browser journey keeps one checkpoint, appends B, adopts B, and restores on reload", async ({
  page
}) => {
  const firstChoices = [
    choice("clarify_frequency_every_night", "每个可用夜晚安排不同夜景", "every_available_evening_distinct", "night_view_frequency"),
    choice("clarify_frequency_once", "两天只安排一次夜景", "one_evening_only", "night_view_frequency")
  ];
  const secondChoices = [
    choice("clarify_style_public_outdoor", "公共开放的户外夜景", "public_outdoor_night_view", "night_view_style"),
    choice("clarify_style_indoor", "室内付费观景平台", "paid_indoor_observation", "night_view_style")
  ];
  const planA = projection("proposal_browser_a", "turn_browser_a", "choice_browser_a", "学府书声映照京城夜色", "a");
  const planB = projection("proposal_browser_b", "turn_browser_b", "choice_browser_b", "校园清韵漫入公园华灯", "b");
  const continueChoice = {
    id: "choice_browser_continue",
    index: 2,
    kind: "portfolio_partial_more_plans",
    action: "retry_model_planning",
    label: "继续生成其他方案",
    lifecycle: "offered",
    planningSelectionRootTurnId: ROOT_TURN_ID,
    rootPortfolioId: PORTFOLIO_ID
  };

  const responses = [
    message(
      turn(ROOT_TURN_ID, "user", 1, USER_REQUEST),
      turn("turn_browser_question_1", "assistant", 2, "两个夜晚都要安排夜景吗？", {
        choiceOptions: firstChoices,
        clarificationCheckpoint: checkpoint([], ["night_view_frequency", "night_view_style"])
      })
    ),
    message(
      turn("turn_browser_frequency", "user", 3, firstChoices[0].label, {
        structuredChoiceTrace: succeededTrace("turn_browser_question_1", firstChoices[0].id)
      }),
      turn("turn_browser_question_2", "assistant", 4, "夜景更偏向哪一种公共体验？", {
        choiceOptions: secondChoices,
        clarificationCheckpoint: checkpoint(["night_view_frequency"], ["night_view_style"])
      })
    ),
    message(
      turn("turn_browser_style", "user", 5, secondChoices[0].label, {
        structuredChoiceTrace: succeededTrace("turn_browser_question_2", secondChoices[0].id)
      }),
      turn("turn_browser_a", "assistant", 6, "方案 A 已完成严格核验。", {
        comparisonProjectionUpdateMode: "replace",
        comparisonProjections: [planA],
        choiceOptions: [proposalChoice(planA), continueChoice],
        clarificationCheckpoint: {
          ...checkpoint(["night_view_frequency", "night_view_style"], []),
          status: "resolved"
        }
      })
    ),
    message(
      turn("turn_browser_continue", "user", 7, continueChoice.label, {
        structuredChoiceTrace: {
          ...succeededTrace("turn_browser_a", continueChoice.id),
          outcome: { versionDelta: 0, patchDelta: 0, routeWriteDelta: 0 }
        }
      }),
      turn("turn_browser_b", "assistant", 8, "方案 B 已追加，方案 A 保持可见。", {
        comparisonProjectionUpdateMode: "append",
        comparisonProjections: [planB],
        choiceOptions: [proposalChoice(planB)]
      })
    ),
    message(
      turn("turn_browser_adopt", "user", 9, "采用此方案", {
        itineraryVersionId: "version_browser_b",
        structuredChoiceTrace: {
          ...succeededTrace("turn_browser_b", planB.choiceId),
          resultVersionId: "version_browser_b",
          outcome: { versionDelta: 1, patchDelta: 1, routeWriteDelta: 2 }
        }
      }),
      turn("turn_browser_adopted", "assistant", 10, "已采用方案 B，并写入正式时间轴。"),
      itinerary(planB),
      { id: "version_browser_b", versionNumber: 1, sourceType: "agent_portfolio_commit" }
    )
  ];

  const persistedTurns: JsonRecord[] = [];
  const requestBodies: JsonRecord[] = [];
  let streamIndex = 0;
  let adopted = false;
  await page.route("**/api/**", async (route) => {
    await handleApiRoute(route, {
      responses,
      persistedTurns,
      requestBodies,
      nextStreamIndex: () => streamIndex++,
      isAdopted: () => adopted,
      markAdopted: () => {
        adopted = true;
      },
      adoptedItinerary: itinerary(planB)
    });
  });

  await page.goto("/", { waitUntil: "domcontentloaded" });
  const input = page.getByRole("textbox", { name: "Agent 对话文本" });
  await input.fill(USER_REQUEST);
  await input.press("Enter");

  const frequency = page.locator('[data-semantic-value="every_available_evening_distinct"]');
  await expect(frequency).toBeVisible();
  await expect(frequency).toHaveAttribute("data-checkpoint-id", CHECKPOINT_ID);
  await frequency.click();

  const outdoor = page.locator('[data-semantic-value="public_outdoor_night_view"]');
  await expect(outdoor).toBeVisible();
  await expect(outdoor).toHaveAttribute("data-checkpoint-id", CHECKPOINT_ID);
  await outdoor.click();

  await expect(page.locator('[data-proposal-id="proposal_browser_a"]')).toBeVisible();
  await page.locator('[data-choice-id="choice_browser_continue"]').click();
  await expect(page.locator("article[data-proposal-id]")).toHaveCount(2);
  await expect(page.locator('[data-proposal-id="proposal_browser_a"]')).toContainText(planA.title);
  await expect(page.locator('[data-proposal-id="proposal_browser_b"]')).toContainText(planB.title);

  await page.getByRole("tab", { name: "行程对比" }).click();
  const planBCard = page.locator('article[data-proposal-id="proposal_browser_b"]');
  const adoptB = planBCard.getByRole("button", { name: "采用此方案", exact: true });
  await adoptB.dblclick();
  await expect.poll(() => requestBodies.length).toBe(5);
  const adoptionRequestCount = () =>
    requestBodies.filter(
      (body) => selectedChoice(body)?.choiceId === "choice_browser_b"
    ).length;
  expect(adoptionRequestCount()).toBe(1);
  expect(selectedChoice(requestBodies[4])).toEqual({
    sourceAssistantTurnId: "turn_browser_b",
    choiceId: "choice_browser_b"
  });

  await page.getByRole("tab", { name: "行程对比" }).click();
  const adoptedB = planBCard.getByRole("button", { name: "已采用", exact: true });
  await expect(adoptedB).toBeDisabled();
  await adoptedB.click({ force: true });
  expect(adoptionRequestCount()).toBe(1);
  await expect(page.locator("body")).toContainText("奥林匹克森林公园");

  await page.reload({ waitUntil: "domcontentloaded" });
  await page.getByRole("tab", { name: "行程对比" }).click();
  await expect(page.locator("article[data-proposal-id]")).toHaveCount(2);
  await expect(page.locator('[data-proposal-id="proposal_browser_a"]')).toContainText(planA.title);
  const reloadedB = page.locator('[data-proposal-id="proposal_browser_b"]');
  await expect(reloadedB).toContainText(planB.title);
  await expect(reloadedB.getByRole("button", { name: "已采用", exact: true })).toBeDisabled();
  await expect(page.locator("body")).toContainText("奥林匹克森林公园");
});

type JsonRecord = Record<string, any>;

async function handleApiRoute(
  route: Route,
  state: {
    responses: JsonRecord[];
    persistedTurns: JsonRecord[];
    requestBodies: JsonRecord[];
    nextStreamIndex: () => number;
    isAdopted: () => boolean;
    markAdopted: () => void;
    adoptedItinerary: JsonRecord;
  }
) {
  const request = route.request();
  const url = new URL(request.url());
  const path = url.pathname;
  const method = request.method();
  if (path.endsWith("/providers/status")) {
    return fulfillJson(route, { mode: "mock", default: [], mock: [] });
  }
  if (path.endsWith("/map/config")) {
    return fulfillJson(route, { enabled: false, jsApiKey: "", securityJsCode: "" });
  }
  if (path.endsWith("/preferences/memory")) {
    return fulfillJson(route, {
      userId: "browser-user",
      memoryText: "# 我的旅行偏好",
      autoUpdateEnabled: true,
      createdAt: CREATED_AT,
      updatedAt: CREATED_AT
    });
  }
  if (path.endsWith("/preferences/extract")) {
    return fulfillJson(route, { summaryCard: preferenceCard() });
  }
  if (path.endsWith("/agent/sessions/current")) {
    if (!state.persistedTurns.length) {
      return fulfillJson(route, { detail: "No active conversation session" }, 404);
    }
    return fulfillJson(route, persistedSession(state));
  }
  if (path.endsWith(`/agent/sessions/${SESSION_ID}`) && method === "GET") {
    return fulfillJson(route, persistedSession(state));
  }
  if (path.endsWith("/agent/sessions") && method === "GET") {
    return fulfillJson(route, { sessions: [] });
  }
  if (path.endsWith("/agent/sessions") && method === "POST") {
    return fulfillJson(route, emptySession());
  }
  if (path.endsWith(`/agent/sessions/${SESSION_ID}/messages/stream`) && method === "POST") {
    state.requestBodies.push((request.postDataJSON() ?? {}) as JsonRecord);
    const index = state.nextStreamIndex();
    const response = state.responses[index];
    if (!response) return fulfillJson(route, { detail: "unexpected stream" }, 500);
    state.persistedTurns.push(response.userTurn, response.assistantTurn);
    if (response.version?.id === "version_browser_b") state.markAdopted();
    return route.fulfill({
      status: 200,
      contentType: "application/x-ndjson",
      body: `${JSON.stringify({ event: "user_turn", data: response.userTurn })}\n${JSON.stringify({
        event: "message_response",
        data: response
      })}\n`
    });
  }
  return fulfillJson(route, {}, 404);
}

function choice(id: string, label: string, semanticValue: string, dimensionId: string) {
  return {
    id,
    index: id.endsWith("once") || id.endsWith("indoor") ? 2 : 1,
    kind: "clarification_checkpoint",
    action: "continue_clarification",
    label,
    semanticValue,
    dimensionId,
    checkpointId: CHECKPOINT_ID
  };
}

function checkpoint(resolvedDimensions: string[], unresolvedDimensions: string[]) {
  return {
    checkpointId: CHECKPOINT_ID,
    status: "active",
    answers: resolvedDimensions.map((dimensionId) => ({
      dimensionId,
      semanticValue:
        dimensionId === "night_view_frequency"
          ? "every_available_evening_distinct"
          : "public_outdoor_night_view"
    })),
    resolvedDimensions,
    unresolvedDimensions,
    experienceSpecs: [],
    candidateGapSummary: { nightViewSlots: 2 }
  };
}

function succeededTrace(sourceAssistantTurnId: string, resolvedChoiceId: string) {
  return { sourceAssistantTurnId, resolvedChoiceId, executionStatus: "succeeded" };
}

function turn(id: string, role: string, turnIndex: number, content: string, extra: JsonRecord = {}) {
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

function message(userTurn: JsonRecord, assistantTurn: JsonRecord, itineraryPlan: JsonRecord | null = null, version: JsonRecord | null = null) {
  return {
    userTurn,
    assistantTurn,
    itinerary: itineraryPlan,
    version,
    pendingPoiCandidates: [],
    warnings: [],
    planningSteps: [],
    toolEvents: [],
    executionMode: "bounded_agent",
    terminalStatus: itineraryPlan ? "success" : "needs_confirmation",
    agentDecisionCount: 1,
    outcomeStatuses: {}
  };
}

function projection(proposalId: string, sourceAssistantTurnId: string, choiceId: string, title: string, seed: "a" | "b") {
  const places = seed === "a"
    ? [["清华大学", "B000A0001", 40.003, 116.326], ["中央电视塔", "B000A0002", 39.918, 116.300], ["北京大学", "B000A0003", 39.992, 116.305], ["奥林匹克塔", "B000A0004", 40.012, 116.393]]
    : [["中国人民大学", "B000B0001", 39.969, 116.321], ["景山公园", "B000B0002", 39.925, 116.397], ["北京师范大学", "B000B0003", 39.962, 116.366], ["奥林匹克森林公园", "B000B0004", 40.016, 116.389]];
  const days = [1, 2].map((dayNumber) => ({
    id: `${proposalId}_day_${dayNumber}`,
    dayNumber,
    title: `第 ${dayNumber} 天高校与公共夜景`,
    weatherSummary: "晴",
    riskSummary: "路线已核验",
    totalEstimatedCost: 80,
    pendingSlots: [],
    segments: [
      segment(proposalId, dayNumber, "campus", places[(dayNumber - 1) * 2], "09:00", "campus_visit"),
      segment(proposalId, dayNumber, "night", places[(dayNumber - 1) * 2 + 1], "19:00", "night_view")
    ]
  }));
  const routeEvidence = days.map((day) => {
    const [from, to] = day.segments;
    return {
      id: `${proposalId}_route_${day.dayNumber}`,
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
      polyline: [[from.poi.longitude, from.poi.latitude], [to.poi.longitude, to.poi.latitude]],
      steps: [],
      providerPayload: { status: "1" },
      queriedAt: CREATED_AT
    };
  });
  return {
    planningSelectionRootTurnId: ROOT_TURN_ID,
    rootPortfolioId: PORTFOLIO_ID,
    proposalId,
    sourceAssistantTurnId,
    choiceId,
    status: "complete",
    isPartial: false,
    isAdopted: false,
    adoptionReady: true,
    strictlyVerified: true,
    structureReady: true,
    adoptionMode: "complete",
    activeVersionId: null,
    expectedBaseVersionId: null,
    title,
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
    tradeoffSummary: seed === "a" ? "经典高校与城市高点" : "社区高校与公共公园夜色",
    colorKey: seed === "a" ? "ocean" : "amber"
  };
}

function segment(proposalId: string, dayNumber: number, role: string, place: Array<string | number>, startTime: string, intentType: string) {
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
    semanticMetadata: { routeAnchor: true, intentType, requirementLevel: "hard", groundingStatus: "selected" }
  };
}

function itinerary(plan: JsonRecord) {
  return {
    id: "plan_browser_b",
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

function proposalChoice(plan: JsonRecord) {
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

function emptySession() {
  return {
    sessionId: SESSION_ID,
    status: "active",
    city: "北京",
    title: "北京高校与夜景",
    activePlanId: "plan_browser",
    activeVersionId: null,
    turns: [],
    itinerary: null,
    pendingPoiCandidates: []
  };
}

function persistedSession(state: {
  persistedTurns: JsonRecord[];
  isAdopted: () => boolean;
  adoptedItinerary: JsonRecord;
}) {
  return {
    ...emptySession(),
    activePlanId: state.isAdopted() ? state.adoptedItinerary.id : "plan_browser",
    activeVersionId: state.isAdopted() ? "version_browser_b" : null,
    turns: state.persistedTurns,
    itinerary: state.isAdopted() ? state.adoptedItinerary : null
  };
}

function preferenceCard() {
  return {
    id: "card_browser",
    profileId: "pref_browser",
    partySize: 1,
    travelerTypes: ["adult"],
    budgetRange: "中等预算",
    pacePreference: "标准",
    summaryText: "公交地铁优先",
    items: [{ label: "公交地铁优先", sourceText: "公交地铁优先" }],
    status: "draft"
  };
}

function selectedChoice(body: JsonRecord) {
  return body.context?.selectedAgentChoice;
}

async function fulfillJson(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

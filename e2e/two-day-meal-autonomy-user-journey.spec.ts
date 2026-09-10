import { expect, test, type Page, type Response } from "@playwright/test";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";

import { assertLatestClarificationSubmissionSucceeded } from "./support/clarification-dimension-contract";
import { awaitingClarificationBatchCards } from "./support/clarification-card-selectors";

const USER_REQUEST =
  "今年国庆，10月1日，10月2日两天，打算一个人去北京的985大学旅游，每天参观一所不同的985高校，每天中午品尝不同的当地网红美食，每天晚上去当日附近公共开放的公园或滨水夜景散步";
const MAX_ADJACENT_POI_METERS = 8_000;
const MAX_TRANSIT_ROUTE_SECONDS = 60 * 60;
const CONTROLLED_NIGHT_PATTERN =
  /欢乐谷|环球影城|主题乐园|摩天轮|中央电视塔|奥林匹克塔|中国尊|中信大厦/;
const CAMPUS_PROVIDER_PATTERN =
  /科教文化|学校|高等院校|campus|education|university/i;
const CAMPUS_NON_ENTITY_PATTERN =
  /附近|周边|商场|购物|餐厅|酒店|公寓|地铁|公交|医院|科技园|产业园/;
const CAMPUS_SUBENTITY_PATTERN =
  /校本部|本部|校区|校园|校门|东门|西门|南门|北门|工字厅|主楼|教学楼|图书馆|礼堂|医学部|学院/;
const NIGHT_PLACEHOLDER_PATTERN =
  /待补|待定|附近范围|周边范围|候选|placeholder|pending/i;
const NON_PUBLIC_NIGHT_FACILITY_PATTERN =
  /观景台|观景平台|瞭望台|观景塔|展望塔/;
const PUBLIC_NIGHT_PATTERN =
  /公园|河|湖|滨水|水岸|河畔|湖畔|步道|湿地|园林|什刹海|后海/i;
const API_BASE =
  process.env.TRIP_E2E_API_BASE_URL || "http://localhost:8000/api";
const STREAM_PATH = /\/api\/agent\/sessions\/[^/]+\/messages\/stream(?:\?|$)/;

type JsonRecord = Record<string, unknown>;

test("真实用户动态确认路线偏好后生成两日高校、午间美食和晚间公园行程", async ({
  context,
  page,
}, testInfo) => {
  const artifactDir = path.resolve(
    process.env.TRIP_E2E_ARTIFACT_DIR || testInfo.outputDir,
  );
  const runId = process.env.TRIP_E2E_RUN_ID || `two-day-meal-${Date.now()}`;
  const gitCommit = String(process.env.TRIP_E2E_GIT_COMMIT || "")
    .trim()
    .toLowerCase();
  expect(gitCommit).toMatch(/^[0-9a-f]{40}$/);
  await mkdir(artifactDir, { recursive: true });

  const streamResponses: Response[] = [];
  page.on("response", (response) => {
    if (
      STREAM_PATH.test(response.url()) &&
      response.request().method() === "POST"
    ) {
      streamResponses.push(response);
    }
  });

  await page.goto("/", { waitUntil: "domcontentloaded" });
  await expect(
    page.getByRole("form", { name: "Agent 对话输入" }),
  ).toBeVisible();

  const initialSessions = await fetchJson(page, `${API_BASE}/agent/sessions`);
  expect(asArray(initialSessions.sessions)).toHaveLength(0);

  const providerStatus = await fetchJson(page, `${API_BASE}/providers/status`);
  const agent = asRecord(providerStatus.agent);
  const tools = asRecord(providerStatus.tools);
  const amapWeather = asRecord(tools.amapWeather);
  expect(providerStatus.mode).toBe("default");
  expect(agent.configured).toBe(true);
  expect(agent.status).not.toBe("unavailable");
  expect(String(agent.providerName || "")).toMatch(/deepseek/i);
  expect(amapWeather.configured).toBe(true);

  const mapConfig = await fetchJson(page, `${API_BASE}/map/config`);
  expect(mapConfig.enabled).toBe(true);
  expect(String(mapConfig.jsApiKey || "").length).toBeGreaterThan(8);

  // Provider and map readiness payloads may contain credentials, so retained
  // tracing begins only after preflight and omits DOM/network snapshots.
  const tracePath = path.join(artifactDir, "trace.zip");
  await context.tracing.start({
    screenshots: false,
    snapshots: false,
    sources: true,
  });

  try {
    const textarea = page.getByRole("textbox", { name: "Agent 对话文本" });
    await textarea.fill(USER_REQUEST);
    await textarea.press("Enter");
    await waitForSettledStreams(page, streamResponses, 1);

    const sessionList = await fetchJson(page, `${API_BASE}/agent/sessions`);
    const sessions = asArray(sessionList.sessions).map(asRecord);
    expect(sessions).toHaveLength(1);
    const sessionId = String(sessions[0].sessionId || sessions[0].id || "");
    expect(sessionId).not.toBe("");

    const offeredSession = await fetchJson(
      page,
      `${API_BASE}/agent/sessions/${sessionId}`,
    );
    expect(String(offeredSession.activeVersionId || "")).toBe("");
    const initialClarification = clarificationEvidence(offeredSession);
    expect(asArray(initialClarification.awaitingCheckpointIds)).toHaveLength(1);
    expect(initialClarification.submissionChoiceCount).toBe(1);

    const offeredBatch = latestAwaitingClarificationBatch(offeredSession);
    const sourceAssistantTurnId = String(offeredBatch.turn.id || "");
    const checkpointId = String(offeredBatch.checkpoint.checkpointId || "");
    const checkpointFingerprint = String(
      offeredBatch.checkpoint.fingerprint || "",
    );
    expect(sourceAssistantTurnId).not.toBe("");
    expect(String(offeredBatch.checkpoint.sourceAssistantTurnId || "")).toBe(
      sourceAssistantTurnId,
    );
    expect(checkpointId).not.toBe("");
    expect(checkpointFingerprint).not.toBe("");

    const questions = asArray(offeredBatch.checkpoint.questions).map(asRecord);
    expect(questions.length).toBeGreaterThanOrEqual(1);
    const dimensions = questions.map((question) =>
      String(question.dimensionId || ""),
    );
    expect(dimensions).toContain("route_decision.mobility_profile");
    expect(dimensions).toContain("route_decision.detour_tolerance");
    expect(new Set(dimensions).size).toBe(dimensions.length);

    const batchCard = awaitingClarificationBatchCards(page);
    await expect(batchCard).toBeVisible();
    const freeTextQuestions = questions.filter(
      (question) => question.allowFreeText === true,
    );
    expect(freeTextQuestions.length).toBeGreaterThanOrEqual(1);
    const inlineInputs = batchCard.locator('input[type="text"]');
    await expect(inlineInputs).toHaveCount(freeTextQuestions.length);
    const inlineInputCount = await inlineInputs.count();

    const manualQuestion =
      freeTextQuestions.find(
        (question) =>
          question.dimensionId === "route_decision.mobility_profile",
      ) ?? freeTextQuestions[0];
    const manualValue = "公共交通为主，步行节奏标准";
    const clarificationSelections: JsonRecord[] = [];
    for (const question of questions) {
      const dimensionId = String(question.dimensionId || "");
      const questionText = String(question.question || "");
      expect(dimensionId).not.toBe("");
      expect(questionText).not.toBe("");
      const options = asArray(question.options).map(asRecord);
      expect(options.length).toBeGreaterThanOrEqual(2);
      expect(
        new Set(options.map((option) => String(option.id || ""))).size,
      ).toBe(options.length);
      const group = batchCard.getByRole("group", {
        name: questionText,
        exact: true,
      });
      await expect(group).toBeVisible();
      await expect(
        group.locator('input[type="radio"][data-option-id]'),
      ).toHaveCount(options.length);

      if (question === manualQuestion) {
        const manualInput = group.getByRole("textbox", {
          name: `${questionText}的补充内容`,
          exact: true,
        });
        await expect(manualInput).toBeVisible();
        await manualInput.fill(manualValue);
        clarificationSelections.push({
          dimensionId,
          submissionMode: "manual_value",
          manualValue,
        });
      } else {
        const option = options[0];
        const optionId = String(option.id || "");
        const label = String(option.label || "");
        expect(optionId).not.toBe("");
        expect(label).not.toBe("");
        await group
          .locator(`input[type="radio"][data-option-id="${optionId}"]`)
          .check();
        clarificationSelections.push({
          dimensionId,
          submissionMode: "persisted_option",
          optionId,
          label,
          semanticValue: option.semanticValue,
        });
      }
    }

    const streamsBeforeClarification = streamResponses.length;
    const submitClarification = batchCard.getByRole("button", {
      name: "确认并开始规划",
      exact: true,
    });
    await expect(submitClarification).toBeEnabled();
    await submitClarification.click();
    await waitForSettledStreams(
      page,
      streamResponses,
      streamsBeforeClarification + 1,
    );
    const resolvedSession = await fetchJson(
      page,
      `${API_BASE}/agent/sessions/${sessionId}`,
    );
    const submissionEvidence = assertLatestClarificationSubmissionSucceeded(
      resolvedSession,
      { checkpointId, checkpointFingerprint, sourceAssistantTurnId },
    );
    const resolvedClarification = clarificationEvidence(resolvedSession);
    expect(resolvedClarification.awaitingCheckpointIds).toEqual([]);
    await expect(awaitingClarificationBatchCards(page)).toHaveCount(0);

    await page.getByRole("tab", { name: "行程对比" }).click();
    const readyCards = page.locator(
      'article[data-proposal-id][data-adoption-ready="true"]',
    );
    await expect
      .poll(() => readyCards.count(), { timeout: 180_000 })
      .toBeGreaterThanOrEqual(1);

    const card = readyCards.first();
    const proposalId = String(
      (await card.getAttribute("data-proposal-id")) || "",
    );
    expect(proposalId).not.toBe("");
    const readyCardCount = await readyCards.count();
    const proposalAriaLabel = await card.getAttribute("aria-label");
    await expect(card.locator(".comparison-day")).toHaveCount(2);
    await expect(card).toContainText("Day 1");
    await expect(card).toContainText("Day 2");

    const proposal = findLatestProjection(resolvedSession, proposalId);
    const proposalEvidence = itinerarySemanticEvidence(proposal);
    assertRequestedJourney(proposalEvidence);

    const confirmButton = card.getByRole("button", {
      name: /^确认编辑「.+」$/,
    });
    await expect(confirmButton).toBeVisible();
    await expect(confirmButton).toBeEnabled();
    const streamsBeforeAdoption = streamResponses.length;
    await confirmButton.click();
    await waitForSettledStreams(
      page,
      streamResponses,
      streamsBeforeAdoption + 1,
    );

    await expect(page.locator(".map-stage")).toHaveAttribute(
      "data-map-mode",
      "itinerary_edit",
      { timeout: 120_000 },
    );
    await expect(
      page.getByRole("region", { name: "Day 1 itinerary group" }),
    ).toBeVisible();
    await expect(
      page.getByRole("region", { name: "Day 2 itinerary group" }),
    ).toBeVisible();

    const adoptedSession = await fetchJson(
      page,
      `${API_BASE}/agent/sessions/${sessionId}`,
    );
    const activeVersionId = String(adoptedSession.activeVersionId || "");
    expect(activeVersionId).not.toBe("");
    const adoptedEvidence = itinerarySemanticEvidence(
      asRecord(adoptedSession.itinerary),
    );
    assertRequestedJourney(adoptedEvidence);

    await page.reload({ waitUntil: "domcontentloaded" });
    await expect(
      page.getByRole("form", { name: "Agent 对话输入" }),
    ).toBeVisible();
    await expect(
      page.getByRole("region", { name: "Day 1 itinerary group" }),
    ).toBeVisible({ timeout: 60_000 });
    await expect(
      page.getByRole("region", { name: "Day 2 itinerary group" }),
    ).toBeVisible();
    const reloadedSession = await fetchJson(
      page,
      `${API_BASE}/agent/sessions/${sessionId}`,
    );
    expect(String(reloadedSession.activeVersionId || "")).toBe(activeVersionId);

    const result = {
      schemaVersion: "trip-two-day-quality-live-v3",
      runId,
      gitCommit,
      sessionId,
      proposalId,
      activeVersionId,
      userRequest: USER_REQUEST,
      providerReadiness: {
        mode: providerStatus.mode,
        agentProviderName: String(agent.providerName || ""),
        agentConfigured: agent.configured,
        amapConfigured: amapWeather.configured,
        browserMapEnabled: mapConfig.enabled,
      },
      clarification: {
        initial: initialClarification,
        resolved: resolvedClarification,
        checkpointId,
        checkpointFingerprint,
        sourceAssistantTurnId,
        questionCount: questions.length,
        dimensions,
        freeTextQuestionCount: freeTextQuestions.length,
        inlineInputCount,
        selections: clarificationSelections,
        submissionEvidence,
      },
      proposalEvidence,
      adoptedEvidence,
      streamRequestCount: streamResponses.length,
      browserEvidence: {
        readyCardCount,
        proposalAriaLabel,
        dayGroupCountAfterReload: await page
          .locator("section.day-card")
          .count(),
        reloadPreservedActiveVersion: true,
      },
    };
    await writeFile(
      path.join(artifactDir, "journey-result.json"),
      JSON.stringify(result, null, 2),
      "utf8",
    );
    await page.screenshot({
      path: path.join(artifactDir, "two-day-adopted-itinerary.png"),
      fullPage: true,
    });
  } finally {
    await context.tracing.stop({ path: tracePath });
  }
});

function assertRequestedJourney(evidence: JsonRecord) {
  const days = asArray(evidence.days).map(asRecord);
  const segments = asArray(evidence.segments).map(asRecord);
  expect(days).toHaveLength(2);
  expect(days.map((day) => String(day.date || ""))).toEqual([
    "2026-10-01",
    "2026-10-02",
  ]);

  const campus = segments.filter((segment) => segment.intentType === "campus");
  expect(campus.map((segment) => Number(segment.dayNumber)).sort()).toEqual([
    1, 2,
  ]);
  expect(new Set(campus.map((segment) => String(segment.amapId))).size).toBe(2);
  for (const segment of campus) {
    const binding = asRecord(segment.qualificationBinding);
    expect(binding.schemaVersion).toBe("entity-qualification-binding-v1");
    expect(binding.qualificationScheme).toBe("moe_project_classification");
    expect(binding.qualificationValue).toBe("985");
    expect(String(binding.canonicalName || "")).not.toBe("");
    expect(String(binding.qualificationEvidenceFingerprint || "")).toMatch(
      /^[0-9a-f]{64}$/,
    );
    expect(String(binding.evidenceEntityFingerprint || "")).toMatch(
      /^[0-9a-f]{64}$/,
    );
    expect(String(binding.bindingFingerprint || "")).toMatch(/^[0-9a-f]{64}$/);
    expect(
      `${String(segment.poiType || "")} ${String(segment.poiCategory || "")}`,
    ).toMatch(CAMPUS_PROVIDER_PATTERN);
    const candidateName = normalizeEntity(segment.poiName);
    const canonicalName = normalizeEntity(binding.canonicalName);
    expect(candidateName).not.toMatch(CAMPUS_NON_ENTITY_PATTERN);
    expect(
      candidateName === canonicalName ||
        (candidateName.startsWith(canonicalName) &&
          CAMPUS_SUBENTITY_PATTERN.test(
            candidateName.slice(canonicalName.length),
          )),
    ).toBe(true);
  }
  const meals = segments.filter((segment) => segment.intentType === "meal");
  expect(meals.map((segment) => Number(segment.dayNumber)).sort()).toEqual([
    1, 2,
  ]);
  expect(new Set(meals.map((segment) => String(segment.amapId))).size).toBe(2);
  for (const meal of meals) {
    const start = clockMinutes(String(meal.startTime || ""));
    expect(start).toBeGreaterThanOrEqual(11 * 60);
    expect(start).toBeLessThan(14 * 60);
  }
  const nights = segments.filter((segment) =>
    ["park", "night_view"].includes(String(segment.intentType || "")),
  );
  expect(nights.map((segment) => Number(segment.dayNumber)).sort()).toEqual([
    1, 2,
  ]);
  expect(new Set(nights.map((segment) => String(segment.amapId))).size).toBe(2);
  for (const night of nights) {
    expect(clockMinutes(String(night.startTime || ""))).toBeGreaterThanOrEqual(
      17 * 60,
    );
    expect(String(night.poiName || "")).not.toMatch(CONTROLLED_NIGHT_PATTERN);
    expect(String(night.poiName || "").trim()).not.toBe("");
    expect(String(night.poiName || "")).not.toMatch(NIGHT_PLACEHOLDER_PATTERN);
    expect(
      `${String(night.poiName || "")} ${String(night.poiType || "")} ${String(night.poiCategory || "")}`,
    ).not.toMatch(NON_PUBLIC_NIGHT_FACILITY_PATTERN);
    expect(night.groundingStatus).toBe("verified_amap");
    expect(
      `${String(night.poiName || "")} ${String(night.poiType || "")} ${String(night.poiCategory || "")}`,
    ).toMatch(PUBLIC_NIGHT_PATTERN);
  }
  for (const meal of meals) {
    for (const night of nights.filter(
      (candidate) => candidate.dayNumber === meal.dayNumber,
    )) {
      expect(clockMinutes(String(meal.startTime || ""))).toBeLessThan(
        clockMinutes(String(night.startTime || "")),
      );
    }
  }
  const actualAdjacentPairs: string[] = [];
  for (const dayNumber of [1, 2]) {
    const daySegments = segments
      .filter((segment) => Number(segment.dayNumber) === dayNumber)
      .sort(
        (left, right) =>
          Number(left.segmentOrder || 0) - Number(right.segmentOrder || 0),
      );
    expect(daySegments.map((segment) => String(segment.intentType))).toEqual([
      "campus",
      "meal",
      expect.stringMatching(/^(park|night_view)$/),
    ]);
    for (let index = 1; index < daySegments.length; index += 1) {
      actualAdjacentPairs.push(
        `${String(daySegments[index - 1].amapId || "").toUpperCase()}->${String(daySegments[index].amapId || "").toUpperCase()}`,
      );
      expect(
        haversineMeters(daySegments[index - 1], daySegments[index]),
      ).toBeLessThanOrEqual(MAX_ADJACENT_POI_METERS);
    }
  }
  const routeContract = asRecord(evidence.routeDecisionContract);
  const routeAudit = asRecord(evidence.simpleOpenRouteAssignment);
  expect(routeAudit.schemaVersion).toBe("simple-open-route-evidence-v2");
  expect(String(routeContract.fingerprint || "")).toMatch(/^[0-9a-f]{64}$/);
  expect(routeAudit.routeContractFingerprint).toBe(routeContract.fingerprint);
  expect(routeAudit.routeCoverageComplete).toBe(true);
  expect(routeAudit.adjacentLegCompliance).toBe("verified");
  expect(routeAudit.topologyCompliance).toBe("verified");
  const expectedPairs = asArray(routeAudit.expectedPairs).map(asRecord);
  const verifiedPairs = asArray(routeAudit.verifiedPairs).map(asRecord);
  expect(expectedPairs).toHaveLength(4);
  expect(actualAdjacentPairs).toHaveLength(4);
  expect(new Set(actualAdjacentPairs).size).toBe(4);
  expect(expectedPairs.map(routePairIdentity)).toEqual(actualAdjacentPairs);
  expect(verifiedPairs.map(routePairIdentity)).toEqual(actualAdjacentPairs);
  const adjacentContract = asRecord(routeContract.adjacentLegConstraint);
  const contractLimitSeconds =
    Number(adjacentContract.maxProviderTravelMinutes || 60) * 60;
  const durationLimitSeconds = Math.min(
    MAX_TRANSIT_ROUTE_SECONDS,
    contractLimitSeconds,
  );
  for (const pair of verifiedPairs) {
    const durationSeconds = Number(pair.durationSeconds);
    const distanceMeters = Number(pair.distanceMeters);
    expect(Number.isFinite(durationSeconds)).toBe(true);
    expect(durationSeconds).toBeGreaterThan(0);
    expect(durationSeconds).toBeLessThanOrEqual(durationLimitSeconds);
    expect(Number.isFinite(distanceMeters)).toBe(true);
    expect(distanceMeters).toBeGreaterThan(0);
    expect(["transit", "public_transit"]).toContain(
      String(pair.transportMode || ""),
    );
  }
  for (const segment of segments) {
    expect(String(segment.amapId || "").toUpperCase()).toMatch(
      /^B[0-9A-Z]{8,31}$/,
    );
    expect(segment.poiSource).toBe("amap-place-search");
  }
}

function itinerarySemanticEvidence(container: JsonRecord): JsonRecord {
  const days = asArray(container.days)
    .map(asRecord)
    .map((day, dayIndex) => ({
      dayNumber: Number(day.dayNumber || dayIndex + 1),
      date: String(day.date || ""),
    }))
    .sort((left, right) => left.dayNumber - right.dayNumber);
  const segments: JsonRecord[] = [];
  for (const [dayIndex, dayValue] of asArray(container.days).entries()) {
    const day = asRecord(dayValue);
    const dayNumber = Number(day.dayNumber || dayIndex + 1);
    for (const [segmentIndex, segmentValue] of asArray(
      day.segments,
    ).entries()) {
      const segment = asRecord(segmentValue);
      const semantic = asRecord(segment.semanticMetadata);
      const scheduleConstraints = asRecord(semantic.scheduleConstraints);
      const poi = asRecord(segment.poi);
      segments.push({
        dayNumber,
        segmentOrder: segmentIndex + 1,
        segmentId: String(segment.id || ""),
        intentType: normalizedIntent(segment, semantic, poi),
        startTime: String(segment.startTime || ""),
        endTime: String(segment.endTime || ""),
        poiName: String(poi.name || ""),
        amapId: String(poi.amapId || ""),
        poiSource: String(poi.source || ""),
        latitude: Number(poi.latitude),
        longitude: Number(poi.longitude),
        planningSlotId: String(semantic.planningSlotId || ""),
        poiType: String(poi.type || ""),
        poiCategory: String(poi.category || ""),
        qualificationBinding: asRecord(
          semantic.qualificationBinding ||
            scheduleConstraints.qualificationBinding,
        ),
        groundingStatus: String(
          semantic.groundingStatus || poi.groundingStatus || "",
        ),
      });
    }
  }
  return {
    days,
    segments,
    routeDecisionContract: asRecord(container.routeDecisionContract),
    simpleOpenRouteAssignment: asRecord(container.simpleOpenRouteAssignment),
  };
}

function haversineMeters(left: JsonRecord, right: JsonRecord): number {
  const lat1 = Number(left.latitude);
  const lon1 = Number(left.longitude);
  const lat2 = Number(right.latitude);
  const lon2 = Number(right.longitude);
  expect([lat1, lon1, lat2, lon2].every(Number.isFinite)).toBe(true);
  const radians = (value: number) => (value * Math.PI) / 180;
  const deltaLat = radians(lat2 - lat1);
  const deltaLon = radians(lon2 - lon1);
  const value =
    Math.sin(deltaLat / 2) ** 2 +
    Math.cos(radians(lat1)) *
      Math.cos(radians(lat2)) *
      Math.sin(deltaLon / 2) ** 2;
  return 2 * 6_371_000 * Math.asin(Math.min(1, Math.sqrt(value)));
}

function routePairIdentity(value: JsonRecord): string {
  return `${String(value.fromAmapId || "").toUpperCase()}->${String(value.toAmapId || "").toUpperCase()}`;
}

function normalizeEntity(value: unknown): string {
  return String(value || "")
    .replace(/[\s·•・()（）[\]【】]/g, "")
    .toLocaleLowerCase();
}

function normalizedIntent(
  segment: JsonRecord,
  semantic: JsonRecord,
  poi: JsonRecord,
): string {
  const explicit = String(semantic.intentType || poi.intentType || "");
  const category = String(poi.category || "");
  const name = String(poi.name || "");
  if (explicit === "meal" || category === "food") return "meal";
  if (explicit === "park" || /公园/.test(name)) return "park";
  if (
    explicit === "campus" ||
    /campus|education/.test(category) ||
    /大学|学院|校区/.test(name)
  ) {
    return "campus";
  }
  return explicit || String(segment.kind || "");
}

function clarificationEvidence(session: JsonRecord): JsonRecord {
  const awaitingCheckpointIds: string[] = [];
  const observedAwaitingCheckpointIds: string[] = [];
  const dimensionIds = new Set<string>();
  let submissionChoiceCount = 0;
  let offeredSubmissionChoiceCount = 0;
  for (const turnValue of asArray(session.turns)) {
    const turn = asRecord(turnValue);
    const checkpoint = asRecord(turn.clarificationCheckpoint);
    const status = String(checkpoint.status || "");
    const optionValues = asArray(turn.choiceOptions).length
      ? asArray(turn.choiceOptions)
      : asArray(turn.nextActions);
    const submitOptions = optionValues
      .map(asRecord)
      .filter((option) => option.action === "submit_clarification_batch");
    const offeredSubmitOptions = submitOptions.filter(
      (option) => !option.lifecycle || option.lifecycle === "offered",
    );
    if (["awaiting_answer", "awaiting_agent_resolution"].includes(status)) {
      const identity = String(checkpoint.checkpointId || turn.id || "");
      observedAwaitingCheckpointIds.push(identity);
      if (offeredSubmitOptions.length > 0) awaitingCheckpointIds.push(identity);
    }
    for (const questionValue of asArray(checkpoint.questions)) {
      const question = asRecord(questionValue);
      const dimensionId = String(question.dimensionId || "");
      if (dimensionId) dimensionIds.add(dimensionId);
    }
    submissionChoiceCount += submitOptions.length;
    offeredSubmissionChoiceCount += offeredSubmitOptions.length;
  }
  return {
    awaitingCheckpointIds,
    observedAwaitingCheckpointIds,
    dimensionIds: [...dimensionIds].sort(),
    submissionChoiceCount,
    offeredSubmissionChoiceCount,
  };
}

function latestAwaitingClarificationBatch(session: JsonRecord): {
  turn: JsonRecord;
  checkpoint: JsonRecord;
} {
  const turns = asArray(session.turns).map(asRecord);
  for (let index = turns.length - 1; index >= 0; index -= 1) {
    const turn = turns[index];
    const checkpoint = asRecord(turn.clarificationCheckpoint);
    if (
      checkpoint.schemaVersion === "clarification-checkpoint-v2" &&
      checkpoint.status === "awaiting_answer" &&
      asArray(checkpoint.questions).length > 0
    ) {
      return { turn, checkpoint };
    }
  }
  throw new Error(
    `awaiting dynamic clarification batch missing: ${JSON.stringify(
      turns.map((turn) => ({
        id: turn.id,
        role: turn.role,
        checkpointStatus: asRecord(turn.clarificationCheckpoint).status,
      })),
    )}`,
  );
}

function findLatestProjection(
  session: JsonRecord,
  proposalId: string,
): JsonRecord {
  const turns = asArray(session.turns);
  for (let turnIndex = turns.length - 1; turnIndex >= 0; turnIndex -= 1) {
    const turn = asRecord(turns[turnIndex]);
    const projections = asArray(turn.comparisonProjections);
    for (
      let projectionIndex = projections.length - 1;
      projectionIndex >= 0;
      projectionIndex -= 1
    ) {
      const projection = asRecord(projections[projectionIndex]);
      if (String(projection.proposalId || projection.id || "") === proposalId) {
        return projection;
      }
    }
  }
  throw new Error(`comparison projection missing for proposal ${proposalId}`);
}

async function waitForSettledStreams(
  page: Page,
  responses: Response[],
  minimumCount: number,
) {
  await expect
    .poll(() => responses.length, { timeout: 90_000 })
    .toBeGreaterThanOrEqual(minimumCount);
  let stablePasses = 0;
  let lastCount = -1;
  while (stablePasses < 3) {
    const current = [...responses];
    const errors = await Promise.all(
      current.map((response) => response.finished()),
    );
    expect(errors.filter(Boolean)).toEqual([]);
    for (const response of current) expect(response.status()).toBe(200);
    await page.waitForTimeout(800);
    const composerEnabled = await page
      .getByRole("textbox", { name: "Agent 对话文本" })
      .isEnabled();
    if (
      responses.length === current.length &&
      responses.length === lastCount &&
      composerEnabled
    ) {
      stablePasses += 1;
    } else {
      stablePasses = 0;
    }
    lastCount = responses.length;
  }
}

async function fetchJson(page: Page, url: string): Promise<JsonRecord> {
  return page.evaluate(async (target) => {
    const response = await fetch(target);
    if (!response.ok) throw new Error(`${target} returned ${response.status}`);
    return response.json();
  }, url);
}

function clockMinutes(value: string): number {
  const match = /^(\d{2}):(\d{2})$/.exec(value);
  expect(match).not.toBeNull();
  return Number(match?.[1] || 0) * 60 + Number(match?.[2] || 0);
}

function asRecord(value: unknown): JsonRecord {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as JsonRecord)
    : {};
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

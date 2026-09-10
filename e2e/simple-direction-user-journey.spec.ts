import {
  expect,
  test,
  type Page,
  type Request,
  type Response,
  type Locator,
} from "@playwright/test";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";

import {
  assertLatestClarificationSubmissionSucceeded,
  validateClarificationDimensionIdentitySet,
} from "./support/clarification-dimension-contract";
import {
  awaitingClarificationBatchCards,
  submittedClarificationHistoryCards,
} from "./support/clarification-card-selectors";
import {
  extractExactSelectedAgentChoice,
  readLatestSimpleDirectionLiveState,
  selectStrictDetourOption,
  type CapturedStreamRequest,
  type SimpleDirectionLiveState,
} from "./support/simple-direction-live-state";

const USER_REQUEST =
  "今年国庆参观北京高校两日游，晚上看北京夜景。10月1日到2日，中等预算，1人。每天午餐想体验当地特色美食。";
const EDIT_REQUEST = "把第一天上午的第一站开始时间改为08:00，其他安排不变。";
const EXPECTED_CLARIFICATION_DIMENSIONS = [
  "night_view.cardinality",
  "night_view.experience_mode",
] as const;
const CLARIFICATION_OPTION_PATTERNS: Record<string, RegExp> = {
  "night_view.cardinality": /"frequency":1/,
  "night_view.experience_mode":
    /"experienceFamilies":\["(?:public_city_view|city_landmark_view|waterfront_evening)"\]/,
};
const PUBLIC_XHS_URL =
  "https://fe.xiaohongshu.com/ditto/vincent/286dff38e39b4daf8c6647516f64e512?fullscreen=true&naviHidden=yes";
const RESTRICTED_XHS_URL =
  "https://www.xiaohongshu.com/explore/000000000000000000000000";
const API_BASE =
  process.env.TRIP_E2E_API_BASE_URL || "http://localhost:8000/api";
const STREAM_PATH = /\/api\/agent\/sessions\/[^/]+\/messages\/stream(?:\?|$)/;
const SAVE_PATH =
  /\/api\/agent\/sessions\/[^/]+\/directions\/[^/]+\/save-active(?:\?|$)/;
const SOCIAL_LINK_PATH = /\/api\/source-materials\/social-link(?:\?|$)/;
const VISIT_FACTS_PATH =
  /\/api\/itineraries\/[^/]+\/visit-facts\/refresh(?:\?|$)/;
const PROPOSAL_VISIT_FACTS_PATH =
  /\/api\/agent\/sessions\/[^/]+\/plan-proposals\/[^/]+\/visit-facts\/refresh(?:\?|$)/;
const MAX_PRE_ADOPTION_CONTINUATIONS = 3;

type JsonRecord = Record<string, unknown>;

test("真实用户顺序探索单方向并在 A/B 间保存切换", async ({
  page,
  context,
}, testInfo) => {
  const artifactDir = path.resolve(
    process.env.TRIP_E2E_ARTIFACT_DIR || testInfo.outputDir,
  );
  const runId = process.env.TRIP_E2E_RUN_ID || `playwright-${Date.now()}`;
  const gitCommit = String(process.env.TRIP_E2E_GIT_COMMIT || "")
    .trim()
    .toLowerCase();
  expect(gitCommit).toMatch(/^[0-9a-f]{40}$/);
  await mkdir(artifactDir, { recursive: true });

  const streamResponses: Response[] = [];
  const saveResponses: Response[] = [];
  const socialLinkResponses: Response[] = [];
  const visitFactsResponses: Response[] = [];
  const proposalVisitFactsResponses: Response[] = [];
  const streamRequests: CapturedStreamRequest[] = [];
  page.on("request", (request) => {
    const captured = captureStreamRequest(request);
    if (captured) streamRequests.push(captured);
  });
  page.on("response", (response) => {
    if (
      STREAM_PATH.test(response.url()) &&
      response.request().method() === "POST"
    ) {
      streamResponses.push(response);
    }
    if (
      SAVE_PATH.test(response.url()) &&
      response.request().method() === "POST"
    ) {
      saveResponses.push(response);
    }
    if (
      SOCIAL_LINK_PATH.test(response.url()) &&
      response.request().method() === "POST"
    ) {
      socialLinkResponses.push(response);
    }
    if (
      VISIT_FACTS_PATH.test(response.url()) &&
      response.request().method() === "POST"
    ) {
      visitFactsResponses.push(response);
    }
    if (
      PROPOSAL_VISIT_FACTS_PATH.test(response.url()) &&
      response.request().method() === "POST"
    ) {
      proposalVisitFactsResponses.push(response);
    }
  });

  await page.goto("/", { waitUntil: "domcontentloaded" });
  await expect(
    page.getByRole("form", { name: "Agent 对话输入" }),
  ).toBeVisible();

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

  // Configuration responses can contain map credentials. Start the retained
  // trace only after the one reload used for history verification; that reload
  // fetches map config again. Keep snapshots/screenshots disabled as defense in depth.
  const tracePath = path.join(artifactDir, "trace.zip");
  let traceStarted = false;
  try {
    const textarea = page.getByRole("textbox", { name: "Agent 对话文本" });
    await textarea.fill(USER_REQUEST);
    await textarea.press("Enter");
    await waitForSettledStreams(page, streamResponses, 1);

    // V2 renders every current 1-3 question batch in one card. Select only
    // the latest server-signed structured controls; every card still produces
    // exactly one browser submission and no manual normalizer call.
    const clarificationBatchEvidence = await completeClarificationBatches(
      page,
      streamResponses,
    );
    expect(
      validateClarificationDimensionIdentitySet(
        clarificationBatchEvidence.dimensions,
        EXPECTED_CLARIFICATION_DIMENSIONS,
      ),
    ).toEqual([...EXPECTED_CLARIFICATION_DIMENSIONS].sort());
    expect(clarificationBatchEvidence.manualDimensions).toEqual([]);
    const clarificationHistoryEvidence =
      await verifyClarificationHistoryAfterReload(page);
    await context.tracing.start({
      screenshots: false,
      snapshots: false,
      sources: true,
    });
    traceStarted = true;
    const initialPlanningRetryEvidence =
      await retryLatestSafeFallbackPlanningOnceIfNeeded(
        page,
        streamResponses,
        streamRequests,
      );

    await page.getByRole("tab", { name: "行程对比" }).click();
    const cards = page.locator(
      'article[data-proposal-id][data-adoption-ready="true"]',
    );
    const preAdoptionContinuationEvidence = await advanceBlockedDirections(
      page,
      streamResponses,
      cards,
      {
        expectedActiveVersionId: "",
        phase: "pre_adoption",
        targetReadyCount: 1,
      },
    );
    await expect.poll(() => cards.count(), { timeout: 120_000 }).toBe(1);
    const proposalA = cards.first();
    const proposalAId = String(
      (await proposalA.getAttribute("data-proposal-id")) || "",
    );
    expect(proposalAId).not.toBe("");
    await expect
      .poll(
        () =>
          proposalVisitFactsResponses.some(
            (response) =>
              response.url().includes(`/plan-proposals/${proposalAId}/`) &&
              response.status() === 200,
          ),
        { timeout: 180_000 },
      )
      .toBe(true);
    await expect
      .poll(() => proposalA.locator(".comparison-segment-time").count())
      .toBeGreaterThanOrEqual(4);
    const offeredSession = await fetchJson(
      page,
      `${API_BASE}/agent/sessions/current`,
    );
    const initialAProjection = findLatestProjection(
      offeredSession,
      proposalAId,
    );
    const initialAHardNight = hardNightBranchEvidence(initialAProjection);
    const pendingA = proposalA.locator(".comparison-pending-slot");
    const pendingNightA = pendingA.filter({ hasText: "夜景" });
    let initialPendingAText: string[] = [];
    if (initialAHardNight.branch === "provider_exhausted_pending") {
      await expect.poll(() => pendingNightA.count()).toBeGreaterThanOrEqual(1);
      await expect(proposalA.getByText(/必选地点：仍缺 [1-9]/)).toBeVisible();
      initialPendingAText = (await pendingNightA.allTextContents()).map(
        (item) => item.trim(),
      );
    } else {
      await expect(pendingNightA).toHaveCount(0);
      await expect(proposalA).toContainText(
        String(initialAHardNight.materialized[0].name || ""),
      );
    }
    await expect(
      proposalA.getByRole("button", { name: /^确认编辑「.+」$/ }),
    ).toBeVisible();

    const sessionId = String(
      offeredSession.sessionId || offeredSession.id || "",
    );
    expect(sessionId).not.toBe("");
    expect(String(offeredSession.activeVersionId || "")).toBe("");

    const guideAdviceEvidence = await requestGuideAdviceWithoutWrites(
      page,
      streamResponses,
      cards,
      offeredSession,
    );
    const guideContinuationBefore = readLatestSimpleDirectionLiveState(
      await fetchJson(page, `${API_BASE}/agent/sessions/current`),
    );
    expect(guideContinuationBefore.capability).not.toBeNull();
    const guideCarrierTurnId = String(
      guideContinuationBefore.capability?.sourceAssistantTurnId || "",
    );
    const guideContinuationChoiceId = String(
      guideContinuationBefore.capability?.choiceId || "",
    );
    expect(guideCarrierTurnId).not.toBe("");
    expect(guideContinuationChoiceId).not.toBe("");
    const streamsBeforeNaturalContinuation = streamResponses.length;
    await textarea.fill("继续生成其他方向方案");
    await textarea.press("Enter");
    await waitForSettledStreams(
      page,
      streamResponses,
      streamsBeforeNaturalContinuation + 1,
    );
    const afterNaturalContinuation = await fetchJson(
      page,
      `${API_BASE}/agent/sessions/current`,
    );
    expect(String(afterNaturalContinuation.activeVersionId || "")).toBe("");
    const latestAssistant = asArray(afterNaturalContinuation.turns)
      .map((turn) => asRecord(turn))
      .filter((turn) => turn.role === "assistant")
      .at(-1);
    expect(String(latestAssistant?.content || "")).not.toContain(
      "澄清问题未通过当前需求合同校验",
    );
    const continuationAfter = readLatestSimpleDirectionLiveState(
      afterNaturalContinuation,
    );
    expect(continuationAfter.sourceAssistantTurnId).not.toBe(
      guideCarrierTurnId,
    );
    expect(String(latestAssistant?.planningSelectionRootTurnId || "")).toBe(
      String(
        guideContinuationBefore.capability?.planningSelectionRootTurnId || "",
      ),
    );

    await proposalA
      .getByRole("button", { name: "查看详情", exact: true })
      .click();
    const mapStage = page.locator(".map-stage");
    await expect(mapStage).toHaveAttribute(
      "data-map-mode",
      "plan_overview_preview",
    );
    await expect(mapStage).toHaveAttribute("data-map-can-search", "false");
    await expect(mapStage).toHaveAttribute("data-map-can-mutate", "false");
    await page.getByRole("tab", { name: "行程对比" }).click();

    const streamsBeforeConfirmA = streamResponses.length;
    await page
      .locator(`article[data-proposal-id="${proposalAId}"]`)
      .getByRole("button", { name: /^确认编辑「.+」$/ })
      .click();
    await waitForSettledStreams(
      page,
      streamResponses,
      streamsBeforeConfirmA + 1,
    );
    await expect(mapStage).toHaveAttribute("data-map-mode", "itinerary_edit", {
      timeout: 120_000,
    });
    const confirmedASession = await fetchJson(
      page,
      `${API_BASE}/agent/sessions/current`,
    );
    const versionAConfirmed = String(confirmedASession.activeVersionId || "");
    expect(versionAConfirmed).not.toBe("");
    const visitFactsEvidence = await refreshVisitFactsWithoutVersionWrite(
      page,
      visitFactsResponses,
      versionAConfirmed,
    );

    const activeAssistantTurns = page.locator(".chat-row.assistant.active");
    const assistantCountBeforeEdit = await activeAssistantTurns.count();
    const streamsBeforeEditA = streamResponses.length;
    await textarea.fill(EDIT_REQUEST);
    await textarea.press("Enter");
    await waitForSettledStreams(page, streamResponses, streamsBeforeEditA + 1);
    await expect
      .poll(() => activeAssistantTurns.count(), { timeout: 30_000 })
      .toBe(assistantCountBeforeEdit + 1);
    const assistantCountAfterEdit = await activeAssistantTurns.count();
    const editAssistantTexts = (
      await activeAssistantTurns
        .nth(assistantCountBeforeEdit)
        .locator(".message-bubble")
        .allTextContents()
    )
      .map((item) => item.trim())
      .filter(Boolean);
    expect(editAssistantTexts).toHaveLength(1);
    expect(editAssistantTexts[0]).toContain("08:00");
    expect(editAssistantTexts[0]).not.toMatch(
      /没有得到可安全执行的唯一结果|当前活动版本保持不变|no_safe_action/i,
    );
    const editedASession = await fetchJson(
      page,
      `${API_BASE}/agent/sessions/current`,
    );
    const versionAEdited = String(editedASession.activeVersionId || "");
    expect(versionAEdited).not.toBe("");
    expect(versionAEdited).not.toBe(versionAConfirmed);
    const editedAFirstStart = page
      .locator("#workspace-pane-timeline button.segment-time-pill strong")
      .first();
    await expect(editedAFirstStart).toHaveText("08:00");

    const savesBeforeA = saveResponses.length;
    await page.getByRole("tab", { name: "行程对比" }).click();
    await waitForFinishedResponses(page, saveResponses, savesBeforeA + 1);
    const savedASession = await fetchJson(
      page,
      `${API_BASE}/agent/sessions/current`,
    );
    expect(String(savedASession.activeVersionId || "")).toBe(versionAEdited);
    await expect.poll(() => cards.count(), { timeout: 30_000 }).toBe(1);
    const savedProposalA = page.locator(
      `article[data-proposal-id="${proposalAId}"]`,
    );
    await expect(savedProposalA).toHaveAttribute("data-adoption-ready", "true");
    const savedAProjection = findLatestProjection(savedASession, proposalAId);
    const savedAHardNight = hardNightBranchEvidence(savedAProjection);
    expect(savedAHardNight).toEqual(initialAHardNight);
    const savedPendingA = savedProposalA.locator(".comparison-pending-slot");
    const savedPendingNightA = savedPendingA.filter({ hasText: "夜景" });
    let savedPendingAText: string[] = [];
    if (savedAHardNight.branch === "provider_exhausted_pending") {
      await expect
        .poll(() => savedPendingNightA.count())
        .toBeGreaterThanOrEqual(1);
      savedPendingAText = (await savedPendingNightA.allTextContents()).map(
        (item) => item.trim(),
      );
    } else {
      await expect(savedPendingNightA).toHaveCount(0);
      await expect(savedProposalA).toContainText(
        String(savedAHardNight.materialized[0].name || ""),
      );
    }

    const directionBContinuationEvidence = await advanceBlockedDirections(
      page,
      streamResponses,
      cards,
      {
        expectedActiveVersionId: versionAEdited,
        phase: "direction_b",
        targetReadyCount: 2,
      },
    );
    await expect.poll(() => cards.count(), { timeout: 120_000 }).toBe(2);
    const proposalIds = await cards.evaluateAll((nodes) =>
      nodes
        .map((node) => node.getAttribute("data-proposal-id") || "")
        .filter(Boolean),
    );
    expect(new Set(proposalIds).size).toBe(2);
    expect(proposalIds).toContain(proposalAId);
    const proposalBId = String(
      proposalIds.find((id) => id !== proposalAId) || "",
    );
    expect(proposalBId).not.toBe("");
    const offeredBSession = await fetchJson(
      page,
      `${API_BASE}/agent/sessions/current`,
    );
    expect(String(offeredBSession.activeVersionId || "")).toBe(versionAEdited);
    const initialBProjection = findLatestProjection(
      offeredBSession,
      proposalBId,
    );
    const initialBHardNight = hardNightBranchEvidence(initialBProjection);
    const initialBProposalCore = projectionBusinessCore(initialBProjection);
    const proposalB = page.locator(
      `article[data-proposal-id="${proposalBId}"]`,
    );
    const pendingB = proposalB.locator(".comparison-pending-slot");
    const pendingNightB = pendingB.filter({ hasText: "夜景" });
    if (initialBHardNight.branch === "provider_exhausted_pending") {
      await expect.poll(() => pendingNightB.count()).toBeGreaterThanOrEqual(1);
    } else {
      await expect(pendingNightB).toHaveCount(0);
      await expect(proposalB).toContainText(
        String(initialBHardNight.materialized[0].name || ""),
      );
    }

    const confirmA = page
      .locator(`article[data-proposal-id="${proposalAId}"]`)
      .getByRole("button", { name: /^确认编辑「.+」$/ });
    const confirmB = proposalB.getByRole("button", {
      name: /^确认编辑「.+」$/,
    });
    await expect(confirmA).toBeVisible();
    await expect(confirmB).toBeVisible();
    expect((await confirmA.textContent())?.trim()).not.toBe(
      (await confirmB.textContent())?.trim(),
    );

    const streamsBeforeConfirmB = streamResponses.length;
    await confirmB.click();
    await waitForSettledStreams(
      page,
      streamResponses,
      streamsBeforeConfirmB + 1,
    );
    await expect(mapStage).toHaveAttribute("data-map-mode", "itinerary_edit", {
      timeout: 120_000,
    });
    const confirmedBSession = await fetchJson(
      page,
      `${API_BASE}/agent/sessions/current`,
    );
    const versionBConfirmed = String(confirmedBSession.activeVersionId || "");
    expect(versionBConfirmed).not.toBe("");
    expect(versionBConfirmed).not.toBe(versionAEdited);

    const savesBeforeB = saveResponses.length;
    await page.getByRole("tab", { name: "行程对比" }).click();
    await waitForFinishedResponses(page, saveResponses, savesBeforeB + 1);
    await expect.poll(() => cards.count(), { timeout: 30_000 }).toBe(2);
    const savedBSession = await fetchJson(
      page,
      `${API_BASE}/agent/sessions/current`,
    );
    expect(String(savedBSession.activeVersionId || "")).toBe(versionBConfirmed);
    const savedBProjection = findLatestProjection(savedBSession, proposalBId);
    const savedBHardNight = hardNightBranchEvidence(savedBProjection);
    const savedBProposalCore = projectionBusinessCore(savedBProjection);
    expect(savedBHardNight).toEqual(initialBHardNight);
    expect(savedBProposalCore).toEqual(initialBProposalCore);

    const streamsBeforeRestoreA = streamResponses.length;
    await page
      .locator(`article[data-proposal-id="${proposalAId}"]`)
      .getByRole("button", { name: /^确认编辑「.+」$/ })
      .click();
    await waitForSettledStreams(
      page,
      streamResponses,
      streamsBeforeRestoreA + 1,
    );
    await expect(mapStage).toHaveAttribute("data-map-mode", "itinerary_edit", {
      timeout: 120_000,
    });
    const restoredASession = await fetchJson(
      page,
      `${API_BASE}/agent/sessions/current`,
    );
    const versionARestored = String(restoredASession.activeVersionId || "");
    expect(versionARestored).not.toBe("");
    expect(versionARestored).not.toBe(versionBConfirmed);
    await expect(editedAFirstStart).toHaveText("08:00");
    const restoredAProjection = findLatestProjection(
      restoredASession,
      proposalAId,
    );
    const restoredAHardNight = hardNightBranchEvidence(restoredAProjection);
    expect(restoredAHardNight).toEqual(savedAHardNight);

    await page.screenshot({
      path: path.join(artifactDir, "restored-direction-a.png"),
      fullPage: true,
    });
    const socialLinkEvidence = await exerciseSocialLinkIngestion(
      page,
      streamResponses,
      socialLinkResponses,
    );

    const result = {
      runId,
      gitCommit,
      sessionId,
      userRequest: USER_REQUEST,
      clarificationBatchEvidence,
      clarificationHistoryEvidence,
      guideAdviceEvidence,
      visitFactsEvidence,
      socialLinkEvidence,
      initialPlanningRetryEvidence,
      preAdoptionContinuationEvidence,
      continuationEvidence: [
        ...preAdoptionContinuationEvidence,
        ...directionBContinuationEvidence,
      ],
      editRequest: EDIT_REQUEST,
      proposalAId,
      proposalBId,
      proposalIds,
      versions: {
        aConfirmed: versionAConfirmed,
        aEdited: versionAEdited,
        bConfirmed: versionBConfirmed,
        aRestored: versionARestored,
      },
      editedAFirstStart: String((await editedAFirstStart.textContent()) || ""),
      assistantEditEvidence: {
        countBefore: assistantCountBeforeEdit,
        countAfter: assistantCountAfterEdit,
        addedTexts: editAssistantTexts,
      },
      pendingDirectionAEvidence: {
        initialTexts: initialPendingAText,
        savedTexts: savedPendingAText,
      },
      hardNightBranchEvidence: {
        a: {
          initial: initialAHardNight,
          saved: savedAHardNight,
          restored: restoredAHardNight,
        },
        b: {
          initial: initialBHardNight,
          saved: savedBHardNight,
        },
      },
      proposalBCoreEvidence: {
        initial: initialBProposalCore,
        saved: savedBProposalCore,
      },
      streamRequestCount: streamResponses.length,
      saveRequestCount: saveResponses.length,
      saves: {
        aActiveVersionBeforeAndAfter: versionAEdited,
        bActiveVersionBeforeAndAfter: versionBConfirmed,
      },
    };
    await writeFile(
      path.join(artifactDir, "journey-result.json"),
      JSON.stringify(result, null, 2),
      "utf8",
    );
  } finally {
    if (traceStarted) {
      await context.tracing.stop({ path: tracePath });
    }
  }
});

async function verifyClarificationHistoryAfterReload(page: Page) {
  const session = await fetchJson(page, `${API_BASE}/agent/sessions/current`);
  const submissions = asArray(session.turns)
    .map((turn) => asRecord(asRecord(turn).clarificationSubmission))
    .filter((submission) => submission.status === "succeeded");
  expect(submissions.length).toBeGreaterThanOrEqual(1);
  const answers = submissions.flatMap((submission) =>
    asArray(submission.answers).map((answer) => asRecord(answer)),
  );
  expect(
    answers.map((answer) => String(answer.dimensionId || "")).sort(),
  ).toEqual([...EXPECTED_CLARIFICATION_DIMENSIONS].sort());

  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(
    page.getByRole("form", { name: "Agent 对话输入" }),
  ).toBeVisible();
  const historicalCards = submittedClarificationHistoryCards(page);
  await expect.poll(() => historicalCards.count()).toBeGreaterThanOrEqual(1);
  await expect(historicalCards.locator('input[type="radio"]')).toHaveCount(0);
  await expect(historicalCards.getByRole("textbox")).toHaveCount(0);
  await expect(
    historicalCards.locator(
      'button[data-choice-action="submit_clarification_batch"]',
    ),
  ).toHaveCount(0);
  for (const answer of answers) {
    const label = String(answer.label || "");
    expect(label).not.toBe("");
    await expect(
      historicalCards.getByText(label, { exact: true }).first(),
    ).toBeVisible();
  }
  return {
    submissionCount: submissions.length,
    answers: answers.map((answer) => ({
      dimensionId: String(answer.dimensionId || ""),
      optionId: String(answer.optionId || ""),
      label: String(answer.label || ""),
      source: String(answer.source || ""),
    })),
  };
}

async function requestGuideAdviceWithoutWrites(
  page: Page,
  streamResponses: Response[],
  cards: ReturnType<Page["locator"]>,
  beforeSession: JsonRecord,
) {
  const beforeVersion = String(beforeSession.activeVersionId || "");
  const beforeProposalIds = await cards.evaluateAll((nodes) =>
    nodes
      .map((node) => node.getAttribute("data-proposal-id") || "")
      .filter(Boolean),
  );
  const buttons = page.locator(
    'button[data-choice-action="search_travel_guide_advice"]:not([disabled])',
  );
  await expect
    .poll(() => buttons.count(), { timeout: 30_000 })
    .toBeGreaterThanOrEqual(1);
  const streamsBefore = streamResponses.length;
  await buttons.last().click();
  await waitForSettledStreams(page, streamResponses, streamsBefore + 1);
  const adviceCard = page.locator('section[aria-label="普通攻略建议"]').last();
  await expect(adviceCard).toBeVisible();
  await expect(adviceCard).toContainText("不会自动写入行程");

  const afterSession = await fetchJson(
    page,
    `${API_BASE}/agent/sessions/current`,
  );
  expect(String(afterSession.activeVersionId || "")).toBe(beforeVersion);
  const afterProposalIds = await cards.evaluateAll((nodes) =>
    nodes
      .map((node) => node.getAttribute("data-proposal-id") || "")
      .filter(Boolean),
  );
  expect(afterProposalIds).toEqual(beforeProposalIds);
  const adviceTurns = asArray(afterSession.turns)
    .map((turn) => asRecord(asRecord(turn).guideAdvice))
    .filter((advice) => Object.keys(advice).length > 0);
  expect(adviceTurns).toHaveLength(1);
  const advice = adviceTurns[0];
  const conclusion = asRecord(advice.conclusion);
  expect(["ready", "partial", "conflicting"]).toContain(
    String(conclusion.status || ""),
  );
  expect(String(conclusion.overview || "")).not.toBe("");
  expect(asArray(conclusion.takeaways).length).toBeGreaterThanOrEqual(1);
  expect(String(advice.evidenceFingerprint || "")).toMatch(/^[0-9a-f]{64}$/);
  const recommendations = asArray(advice.recommendations).map((item) =>
    asRecord(item),
  );
  for (const recommendation of recommendations) {
    expect(recommendation.poiVerificationStatus).toBe("unverified_advice");
  }
  return {
    beforeVersion,
    afterVersion: String(afterSession.activeVersionId || ""),
    beforeProposalIds,
    afterProposalIds,
    queryFingerprint: String(advice.queryFingerprint || ""),
    queriedAt: String(advice.queriedAt || ""),
    recommendationCount: recommendations.length,
    sourceCount: asArray(advice.sourceRefs).length,
  };
}

async function refreshVisitFactsWithoutVersionWrite(
  page: Page,
  visitFactsResponses: Response[],
  expectedVersion: string,
) {
  const panel = page.locator("details.segment-visit-facts").first();
  await expect(panel).toBeVisible({ timeout: 30_000 });
  await panel.locator("summary").click();
  const beforeCount = visitFactsResponses.length;
  await panel.getByRole("button", { name: "刷新到访信息" }).click();
  await expect
    .poll(() => visitFactsResponses.length, { timeout: 180_000 })
    .toBeGreaterThanOrEqual(beforeCount + 1);
  const response = visitFactsResponses[visitFactsResponses.length - 1];
  expect(await response.finished()).toBeNull();
  expect(response.status()).toBe(200);
  const payload = asRecord(await response.json());
  const plan = asRecord(payload.plan);
  const factsBySegment = asRecord(plan.visitFactsBySegment);
  const facts = Object.values(factsBySegment).map((item) => asRecord(item));
  expect(facts.length).toBeGreaterThanOrEqual(1);
  const allowedStatuses = new Set([
    "verified",
    "advisory",
    "unknown",
    "conflicting",
    "failed",
    "not_applicable",
  ]);
  for (const aggregate of facts) {
    const aggregateFacts = asRecord(aggregate.facts);
    for (const key of [
      "openingHours",
      "reservation",
      "ticketPrice",
      "ticketRelease",
    ]) {
      expect(
        allowedStatuses.has(String(asRecord(aggregateFacts[key]).status || "")),
      ).toBe(true);
    }
  }
  const session = await fetchJson(page, `${API_BASE}/agent/sessions/current`);
  expect(String(session.activeVersionId || "")).toBe(expectedVersion);
  return {
    aggregateCount: facts.length,
    activeVersionBefore: expectedVersion,
    activeVersionAfter: String(session.activeVersionId || ""),
    refreshStatuses: facts.map((aggregate) =>
      String(aggregate.refreshStatus || ""),
    ),
    sourceRefCount: facts.reduce(
      (total, aggregate) => total + asArray(aggregate.sourceRefs).length,
      0,
    ),
  };
}

async function exerciseSocialLinkIngestion(
  page: Page,
  streamResponses: Response[],
  socialLinkResponses: Response[],
) {
  const textarea = page.getByRole("textbox", { name: "Agent 对话文本" });
  await page.getByRole("button", { name: "新建对话", exact: true }).click();
  await expect(textarea).toBeEnabled();
  const publicSourceBefore = socialLinkResponses.length;
  const publicStreamsBefore = streamResponses.length;
  await textarea.fill(
    `请读取这个公开小红书分享，并把其中可核验的北京地点转换为一日可编辑行程；无法绑定的地点保持未解决：${PUBLIC_XHS_URL}`,
  );
  await textarea.press("Enter");
  await waitForSettledStreams(page, streamResponses, publicStreamsBefore + 1);
  expect(socialLinkResponses.length).toBe(publicSourceBefore + 1);
  const publicMaterial = asRecord(
    await socialLinkResponses[publicSourceBefore].json(),
  );
  expect(publicMaterial.fetchStatus).toBe("succeeded");
  expect(String(publicMaterial.extractedText || "").length).toBeGreaterThan(40);
  const publicSession = await fetchJson(
    page,
    `${API_BASE}/agent/sessions/current`,
  );
  const publicProjections = asArray(publicSession.turns).flatMap((turn) =>
    asArray(asRecord(turn).comparisonProjections),
  );
  const publicItinerary = asRecord(publicSession.itinerary);
  expect(
    Boolean(publicSession.activeVersionId) ||
      publicProjections.length > 0 ||
      asArray(publicItinerary.days).length > 0,
  ).toBe(true);

  await page.getByRole("button", { name: "新建对话", exact: true }).click();
  await expect(textarea).toBeEnabled();
  const restrictedSourceBefore = socialLinkResponses.length;
  const restrictedStreamsBefore = streamResponses.length;
  await textarea.fill(
    `请读取这个小红书分享并规划北京一日游：${RESTRICTED_XHS_URL}`,
  );
  await textarea.press("Enter");
  await waitForSettledStreams(
    page,
    streamResponses,
    restrictedStreamsBefore + 1,
  );
  expect(socialLinkResponses.length).toBe(restrictedSourceBefore + 1);
  const restrictedMaterial = asRecord(
    await socialLinkResponses[restrictedSourceBefore].json(),
  );
  expect(restrictedMaterial.fetchStatus).toBe("needs_user_material");
  expect(String(restrictedMaterial.extractedText || "")).toBe("");
  const restrictedSession = await fetchJson(
    page,
    `${API_BASE}/agent/sessions/current`,
  );
  await expect(page.getByText(/粘贴分享文字|上传截图/).first()).toBeVisible();
  await page.screenshot({
    path: path.join(
      process.env.TRIP_E2E_ARTIFACT_DIR || ".",
      "restricted-xhs-needs-material.png",
    ),
    fullPage: true,
  });
  return {
    public: {
      url: PUBLIC_XHS_URL,
      sourceMaterialId: String(publicMaterial.sourceMaterialId || ""),
      fetchStatus: String(publicMaterial.fetchStatus || ""),
      canonicalUrl: String(publicMaterial.canonicalUrl || ""),
      extractedTextLength: String(publicMaterial.extractedText || "").length,
      sessionId: String(publicSession.sessionId || publicSession.id || ""),
      activeVersionId: String(publicSession.activeVersionId || ""),
      proposalCount: publicProjections.length,
    },
    restricted: {
      url: RESTRICTED_XHS_URL,
      sourceMaterialId: String(restrictedMaterial.sourceMaterialId || ""),
      fetchStatus: String(restrictedMaterial.fetchStatus || ""),
      failureReason: String(restrictedMaterial.failureReason || ""),
      sessionId: String(
        restrictedSession.sessionId || restrictedSession.id || "",
      ),
    },
  };
}

async function advanceBlockedDirections(
  page: Page,
  responses: Response[],
  readyCards: ReturnType<Page["locator"]>,
  options: {
    expectedActiveVersionId: string;
    phase: string;
    targetReadyCount: number;
  },
) {
  const evidence: JsonRecord[] = [];
  const consumedChoiceIds = new Set<string>();
  for (let round = 1; round <= MAX_PRE_ADOPTION_CONTINUATIONS; round += 1) {
    if ((await readyCards.count()) >= options.targetReadyCount) break;
    const session = await fetchJson(page, `${API_BASE}/agent/sessions/current`);
    expect(String(session.activeVersionId || "")).toBe(
      options.expectedActiveVersionId,
    );
    const continuationState = expectComparisonState(
      readLatestSimpleDirectionLiveState(session),
    );
    if (!continuationState.capability) {
      const summary = continuationState.summary;
      const terminalEvidence = {
        round,
        capabilityAvailable: false,
        stopReason: continuationState.stopReason,
        sourceAssistantTurnId: continuationState.sourceAssistantTurnId,
        frontierStatus: String(summary.frontierStatus || ""),
        blockingLayer: String(summary.blockingLayer || ""),
        lastOutcomeReason: String(summary.lastOutcomeReason || ""),
        adoptionReadyCount: Number(summary.adoptionReadyCount || 0),
        repairablePartialCount: Number(summary.repairablePartialCount || 0),
        remainingQualifiedEntityCount: Number(
          summary.remainingQualifiedEntityCount || 0,
        ),
        remainingPoiPageCount: Number(summary.remainingPoiPageCount || 0),
      };
      evidence.push(terminalEvidence);
      throw new Error(
        `simple_direction_continuation_blocked:${JSON.stringify({ phase: options.phase, ...terminalEvidence })}`,
      );
    }
    const capability = continuationState.capability;
    const preSummary = continuationState.summary;
    const choiceId = String(capability.choiceId || "");
    expect(choiceId).not.toBe("");
    expect(consumedChoiceIds.has(choiceId)).toBe(false);
    const button = page
      .locator(
        `button[data-choice-action="continue_plan_expansion"][data-choice-id="${choiceId}"]`,
      )
      .last();
    await expect(button).toBeVisible();
    await expect(button).toBeEnabled();
    const streamsBefore = responses.length;
    await button.click();
    await waitForSettledStreams(page, responses, streamsBefore + 1);
    expect(responses.length).toBe(streamsBefore + 1);
    const postSession = await fetchJson(
      page,
      `${API_BASE}/agent/sessions/current`,
    );
    expect(String(postSession.activeVersionId || "")).toBe(
      options.expectedActiveVersionId,
    );
    const postContinuationState = expectComparisonState(
      readLatestSimpleDirectionLiveState(postSession),
    );
    const postSummary = postContinuationState.summary;
    const postCapability = postContinuationState.capability;
    consumedChoiceIds.add(choiceId);
    evidence.push({
      phase: options.phase,
      round,
      choiceId,
      lifecycle: capability.lifecycle,
      sourceAssistantTurnId: capability.sourceAssistantTurnId,
      sourceUserTurnId: capability.sourceUserTurnId,
      planningSelectionRootTurnId: capability.planningSelectionRootTurnId,
      rootPortfolioId: capability.rootPortfolioId,
      requestContractFingerprint: capability.requestContractFingerprint,
      workflowMode: capability.workflowMode,
      streamRequestDelta: 1,
      activeVersionBefore: String(session.activeVersionId || ""),
      preFrontierStatus: String(preSummary.frontierStatus || ""),
      preAdoptionReadyCount: Number(preSummary.adoptionReadyCount || 0),
      preRepairablePartialCount: Number(preSummary.repairablePartialCount || 0),
      preRemainingQualifiedEntityCount: Number(
        preSummary.remainingQualifiedEntityCount || 0,
      ),
      preRemainingPoiPageCount: Number(preSummary.remainingPoiPageCount || 0),
      postSourceAssistantTurnId: postContinuationState.sourceAssistantTurnId,
      postFrontierStatus: String(postSummary.frontierStatus || ""),
      postAdoptionReadyCount: Number(postSummary.adoptionReadyCount || 0),
      postRepairablePartialCount: Number(
        postSummary.repairablePartialCount || 0,
      ),
      postRemainingQualifiedEntityCount: Number(
        postSummary.remainingQualifiedEntityCount || 0,
      ),
      postRemainingPoiPageCount: Number(postSummary.remainingPoiPageCount || 0),
      postLastOutcomeReason: String(postSummary.lastOutcomeReason || ""),
      postBlockingLayer: String(postSummary.blockingLayer || ""),
      postCapabilityAvailable: Boolean(postCapability),
      postChoiceId: String(postCapability?.choiceId || ""),
      postRequestContractFingerprint: String(
        postCapability?.requestContractFingerprint || "",
      ),
      postStopReason: String(postContinuationState.stopReason || ""),
      activeVersionAfter: String(postSession.activeVersionId || ""),
    });
    await page.getByRole("tab", { name: "行程对比" }).click();
  }
  if ((await readyCards.count()) < options.targetReadyCount) {
    throw new Error(
      `simple_direction_continuation_budget_exhausted:${JSON.stringify({ phase: options.phase, targetReadyCount: options.targetReadyCount, evidence })}`,
    );
  }
  return evidence;
}

async function retryLatestSafeFallbackPlanningOnceIfNeeded(
  page: Page,
  responses: Response[],
  requests: CapturedStreamRequest[],
): Promise<{
  choiceId: string;
  sourceAssistantTurnId: string;
  stateAfterRetry: "comparison";
  streamRequestDelta: number;
  streamResponseDelta: number;
} | null> {
  const initialSession = await fetchJson(
    page,
    `${API_BASE}/agent/sessions/current`,
  );
  const initialState = readLatestSimpleDirectionLiveState(initialSession);
  if (initialState.kind !== "retry_model_planning") {
    return null;
  }

  const button = await findExactAgentChoiceButton(
    page,
    initialState.capability.sourceAssistantTurnId,
    "retry_model_planning",
    initialState.capability.choiceId,
  );
  await expect(button).toBeVisible();
  await expect(button).toBeEnabled();
  const streamsBefore = responses.length;
  const requestsBefore = requests.length;
  await button.click();
  await waitForSettledStreams(page, responses, streamsBefore + 1);
  expect(responses.length).toBe(streamsBefore + 1);
  const capturedRequests = requests.slice(requestsBefore);
  expect(capturedRequests).toHaveLength(1);
  const captured = capturedRequests[0];
  expect(extractExactSelectedAgentChoice(captured.body)).toEqual({
    sourceAssistantTurnId: initialState.capability.sourceAssistantTurnId,
    choiceId: initialState.capability.choiceId,
  });

  const retriedSession = await fetchJson(
    page,
    `${API_BASE}/agent/sessions/current`,
  );
  const retriedState = readLatestSimpleDirectionLiveState(retriedSession);
  if (retriedState.kind !== "comparison") {
    throw new Error(
      `simple_direction_retry_did_not_reach_comparison:${JSON.stringify({
        stateKind: retriedState.kind,
      })}`,
    );
  }
  return {
    choiceId: initialState.capability.choiceId,
    sourceAssistantTurnId: initialState.capability.sourceAssistantTurnId,
    stateAfterRetry: "comparison",
    streamRequestDelta: capturedRequests.length,
    streamResponseDelta: responses.length - streamsBefore,
  };
}

async function findExactAgentChoiceButton(
  page: Page,
  sourceAssistantTurnId: string,
  action: string,
  choiceId: string,
): Promise<Locator> {
  const groups = page.locator("[data-assistant-response-group]");
  const matchingGroups: Locator[] = [];
  for (let index = 0; index < (await groups.count()); index += 1) {
    const group = groups.nth(index);
    if (
      (await group.getAttribute("data-assistant-response-group")) ===
      sourceAssistantTurnId
    ) {
      matchingGroups.push(group);
    }
  }
  if (matchingGroups.length !== 1) {
    throw new Error("simple_direction_retry_source_turn_dom_mismatch");
  }

  const buttons = matchingGroups[0].locator(
    `button[data-choice-action="${action}"]`,
  );
  const matchingButtons: Locator[] = [];
  for (let index = 0; index < (await buttons.count()); index += 1) {
    const button = buttons.nth(index);
    if ((await button.getAttribute("data-choice-id")) === choiceId) {
      matchingButtons.push(button);
    }
  }
  if (matchingButtons.length !== 1) {
    throw new Error("simple_direction_retry_choice_dom_mismatch");
  }
  return matchingButtons[0];
}

async function completeClarificationBatches(page: Page, responses: Response[]) {
  await expect
    .poll(() => awaitingClarificationBatchCards(page).count(), {
      timeout: 30_000,
    })
    .toBe(1);

  const dimensions: string[] = [];
  const manualDimensions: string[] = [];
  const batches: JsonRecord[] = [];
  for (let batchIndex = 0; batchIndex < 4; batchIndex += 1) {
    const card = awaitingClarificationBatchCards(page);
    if ((await card.count()) === 0) break;
    await expect(card).toHaveCount(1);

    const session = await fetchJson(page, `${API_BASE}/agent/sessions/current`);
    expect(String(session.activeVersionId || "")).toBe("");
    const checkpoint = latestAwaitingBatchCheckpoint(session);
    const questions = asArray(checkpoint.questions).map((value) =>
      asRecord(value),
    );
    expect(checkpoint.schemaVersion).toBe("clarification-checkpoint-v2");
    expect(checkpoint.submissionMode).toBe("batch_atomic");
    expect(questions.length).toBeGreaterThanOrEqual(1);
    expect(questions.length).toBeLessThanOrEqual(3);
    const checkpointId = String(checkpoint.checkpointId || "");
    const checkpointFingerprint = String(checkpoint.fingerprint || "");
    const requestFingerprint = String(checkpoint.requestFingerprint || "");
    const planningRootId = String(checkpoint.planningRootId || "");
    const sourceAssistantTurnId = String(
      checkpoint.sourceAssistantTurnId || "",
    );
    expect(checkpointId).not.toBe("");
    expect(checkpointFingerprint).not.toBe("");
    expect(requestFingerprint).not.toBe("");
    expect(planningRootId).not.toBe("");
    expect(sourceAssistantTurnId).not.toBe("");

    const batchDimensions: string[] = [];
    const selections: JsonRecord[] = [];
    for (const question of questions) {
      const dimensionId = String(question.dimensionId || "");
      const questionText = String(question.question || "");
      expect(dimensionId).not.toBe("");
      expect(questionText).not.toBe("");
      expect(dimensions).not.toContain(dimensionId);
      dimensions.push(dimensionId);
      batchDimensions.push(dimensionId);

      const group = card.getByRole("group", {
        name: questionText,
        exact: true,
      });
      await expect(group).toBeVisible();
      if (dimensionId === "route_decision.detour_tolerance") {
        expect(question.allowFreeText).toBe(false);
        await expect(group.getByRole("textbox")).toHaveCount(0);
        await expect(
          group.getByRole("radio", { name: "其他，我来补充" }),
        ).toHaveCount(0);
      }

      let option: JsonRecord | undefined;
      let boundedSemanticValue: unknown;
      if (dimensionId === "route_decision.detour_tolerance") {
        const selected = selectStrictDetourOption(question);
        option = asArray(question.options)
          .map((value) => asRecord(value))
          .find((candidate) => candidate.id === selected.optionId);
        boundedSemanticValue = selected.semanticValue;
      } else {
        const pattern = CLARIFICATION_OPTION_PATTERNS[dimensionId];
        if (!pattern) {
          throw new Error(`unexpected clarification dimension: ${dimensionId}`);
        }
        option = asArray(question.options)
          .map((value) => asRecord(value))
          .find((candidate) =>
            pattern.test(JSON.stringify(asRecord(candidate.semanticValue))),
          );
        boundedSemanticValue = option?.semanticValue;
      }
      if (!option) {
        throw new Error(
          `required typed option missing: ${dimensionId}; visible=${JSON.stringify(
            question.options,
          )}`,
        );
      }
      const label = String(option.label || "");
      const optionId = String(option.id || "");
      expect(label).not.toBe("");
      expect(optionId).not.toBe("");
      const serverOptionRadios = group.locator(
        'input[type="radio"][data-option-id]',
      );
      await expect(serverOptionRadios).toHaveCount(
        asArray(question.options).length,
      );
      let matchingRadioIndex = -1;
      for (
        let index = 0;
        index < (await serverOptionRadios.count());
        index += 1
      ) {
        if (
          String(
            (await serverOptionRadios
              .nth(index)
              .getAttribute("data-option-id")) || "",
          ) === optionId
        ) {
          expect(matchingRadioIndex).toBe(-1);
          matchingRadioIndex = index;
        }
      }
      expect(matchingRadioIndex).toBeGreaterThanOrEqual(0);
      await serverOptionRadios.nth(matchingRadioIndex).check();
      selections.push({
        checkpointId,
        checkpointFingerprint,
        requestFingerprint,
        planningRootId,
        sourceAssistantTurnId,
        dimensionId,
        optionId,
        semanticValue: boundedSemanticValue,
        submissionMode: "persisted_option",
      });
    }

    const submit = card.getByRole("button", {
      name: "确认并开始规划",
      exact: true,
    });
    await expect(submit).toBeEnabled();
    const domCheckpointId = String(
      (await card.getAttribute("data-checkpoint-id")) || "",
    );
    const sourceTurnId = String(
      (await card.getAttribute("data-source-turn-id")) || "",
    );
    expect(domCheckpointId).toBe(checkpointId);
    expect(sourceTurnId).toBe(sourceAssistantTurnId);
    const streamsBeforeSubmit = responses.length;
    await submit.click();
    await waitForSettledStreams(page, responses, streamsBeforeSubmit + 1);
    expect(responses.length).toBe(streamsBeforeSubmit + 1);
    const refreshedSession = await fetchJson(
      page,
      `${API_BASE}/agent/sessions/current`,
    );
    const submissionEvidence = assertLatestClarificationSubmissionSucceeded(
      refreshedSession,
      {
        checkpointId,
        checkpointFingerprint,
        sourceAssistantTurnId,
      },
    );
    batches.push({
      checkpointId: String(checkpoint.checkpointId || ""),
      checkpointFingerprint,
      requestFingerprint,
      planningRootId,
      sourceAssistantTurnId,
      domCheckpointId,
      sourceTurnId,
      dimensions: batchDimensions,
      selections,
      submissionEvidence,
      streamRequestDelta: 1,
    });
  }

  expect(await awaitingClarificationBatchCards(page).count()).toBe(0);
  expect(batches.length).toBeGreaterThanOrEqual(1);
  return {
    batchSubmitCount: batches.length,
    dimensions,
    manualDimensions,
    batches,
  };
}

function latestAwaitingBatchCheckpoint(session: JsonRecord): JsonRecord {
  const turns = asArray(session.turns);
  for (let index = turns.length - 1; index >= 0; index -= 1) {
    const checkpoint = asRecord(asRecord(turns[index]).clarificationCheckpoint);
    if (
      checkpoint.schemaVersion === "clarification-checkpoint-v2" &&
      checkpoint.status === "awaiting_answer"
    ) {
      return checkpoint;
    }
  }
  throw new Error(
    `current v2 clarification checkpoint missing: turns=${JSON.stringify(
      turns.map((value) => ({
        id: asRecord(value).id,
        schemaVersion: asRecord(asRecord(value).clarificationCheckpoint)
          .schemaVersion,
        status: asRecord(asRecord(value).clarificationCheckpoint).status,
      })),
    )}`,
  );
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
      current.map((response) =>
        Promise.race([
          response.finished(),
          new Promise<Error>((resolve) =>
            setTimeout(
              () =>
                resolve(new Error("stream did not finish within 10 minutes")),
              600_000,
            ),
          ),
        ]),
      ),
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

async function waitForFinishedResponses(
  page: Page,
  responses: Response[],
  minimumCount: number,
) {
  await expect
    .poll(() => responses.length, { timeout: 30_000 })
    .toBeGreaterThanOrEqual(minimumCount);
  const current = [...responses];
  const errors = await Promise.all(
    current.map((response) => response.finished()),
  );
  expect(errors.filter(Boolean)).toEqual([]);
  for (const response of current) expect(response.status()).toBe(200);
  await page.waitForTimeout(500);
}

async function fetchJson(page: Page, url: string): Promise<JsonRecord> {
  return page.evaluate(async (target) => {
    const response = await fetch(target);
    if (!response.ok) throw new Error(`${target} returned ${response.status}`);
    return response.json();
  }, url);
}

function captureStreamRequest(request: Request): CapturedStreamRequest | null {
  if (request.method() !== "POST" || !STREAM_PATH.test(request.url())) {
    return null;
  }
  const rawBody = request.postData() || "";
  let parsed: unknown = {};
  try {
    parsed = JSON.parse(rawBody);
  } catch {
    parsed = {};
  }
  return { body: asRecord(parsed), rawBody, url: request.url() };
}

function expectComparisonState(
  state: SimpleDirectionLiveState,
): Extract<SimpleDirectionLiveState, { kind: "comparison" }> {
  if (state.kind !== "comparison") {
    throw new Error(
      `simple_direction_live_state_not_comparison:${JSON.stringify({
        kind: state.kind,
      })}`,
    );
  }
  return state;
}

function asRecord(value: unknown): JsonRecord {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as JsonRecord)
    : {};
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
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

function projectionBusinessCore(projection: JsonRecord) {
  const segments: JsonRecord[] = [];
  for (const [dayIndex, dayValue] of asArray(projection.days).entries()) {
    const day = asRecord(dayValue);
    const dayNumber = Number(day.dayNumber || dayIndex + 1);
    for (const [segmentIndex, segmentValue] of asArray(
      day.segments,
    ).entries()) {
      const segment = asRecord(segmentValue);
      const semantic = asRecord(segment.semanticMetadata);
      const poi = asRecord(segment.poi);
      segments.push({
        dayNumber,
        segmentIndex: segmentIndex + 1,
        id: String(segment.id || ""),
        kind: String(segment.kind || ""),
        startTime: String(segment.startTime || ""),
        endTime: String(segment.endTime || ""),
        amapId: String(poi.amapId || ""),
        intentType: String(semantic.intentType || poi.intentType || ""),
        planningSlotId: String(semantic.planningSlotId || ""),
      });
    }
  }
  const pendingSource = Array.isArray(projection.portfolioPendingSlots)
    ? projection.portfolioPendingSlots
    : projection.pendingSlots;
  const pending = asArray(pendingSource)
    .map((value) => asRecord(value))
    .map((slot) => ({
      id: String(slot.id || ""),
      goalId: String(slot.goalId || ""),
      occurrenceId: String(slot.occurrenceId || ""),
      planningSlotId: String(slot.planningSlotId || ""),
      dayNumber: Number(slot.dayNumber || 0),
      startTime: String(slot.startTime || ""),
      endTime: String(slot.endTime || ""),
      intentType: String(slot.intentType || ""),
      reasonCode: String(slot.reasonCode || ""),
    }))
    .sort((left, right) =>
      `${left.goalId}:${left.occurrenceId}:${left.planningSlotId}`.localeCompare(
        `${right.goalId}:${right.occurrenceId}:${right.planningSlotId}`,
      ),
    );
  return { segments, pending };
}

function hardNightBranchEvidence(projection: JsonRecord) {
  const materialized: JsonRecord[] = [];
  for (const [dayIndex, dayValue] of asArray(projection.days).entries()) {
    const day = asRecord(dayValue);
    const dayNumber = Number(day.dayNumber || dayIndex + 1);
    for (const segmentValue of asArray(day.segments)) {
      const segment = asRecord(segmentValue);
      const semantic = asRecord(segment.semanticMetadata);
      const poi = asRecord(segment.poi);
      const intentType = String(semantic.intentType || poi.intentType || "");
      if (intentType !== "night_view") continue;
      const goalId = String(semantic.goalId || semantic.sourceGoalId || "");
      const sourceGoalId = String(semantic.sourceGoalId || "");
      const occurrenceId = String(semantic.occurrenceId || "");
      const planningSlotId = String(semantic.planningSlotId || "");
      const poolId = String(semantic.poolId || "");
      const amapId = String(poi.amapId || "").toUpperCase();
      const groundingStatus = String(
        semantic.groundingStatus || poi.groundingStatus || "",
      );
      expect(goalId).not.toBe("");
      expect(sourceGoalId).toBe(goalId);
      expect(occurrenceId).not.toBe("");
      expect(planningSlotId).not.toBe("");
      expect(poolId).not.toBe("");
      expect(amapId).toMatch(/^B[0-9A-Z]{8,31}$/);
      expect(String(poi.name || "")).not.toBe("");
      expect(poi.source).toBe("amap-place-search");
      expect(Number.isFinite(Number(poi.latitude))).toBe(true);
      expect(Number.isFinite(Number(poi.longitude))).toBe(true);
      expect(["verified_amap", "provisional"]).toContain(groundingStatus);
      materialized.push({
        goalId,
        sourceGoalId,
        occurrenceId,
        planningSlotId,
        poolId,
        dayNumber,
        startTime: String(segment.startTime || ""),
        endTime: String(segment.endTime || ""),
        amapId,
        name: String(poi.name || ""),
        source: String(poi.source || ""),
        groundingStatus,
      });
    }
  }

  const pendingSource = Array.isArray(projection.portfolioPendingSlots)
    ? projection.portfolioPendingSlots
    : projection.pendingSlots;
  const providerExhaustedPending = asArray(pendingSource)
    .map((value) => asRecord(value))
    .filter(
      (slot) =>
        slot.intentType === "night_view" &&
        ["required", "hard"].includes(String(slot.requirementLevel || "")) &&
        slot.simpleDirectionProviderExhausted === true,
    )
    .map((slot) => {
      const goalId = String(slot.goalId || "");
      const sourceGoalId = String(slot.sourceGoalId || "");
      const occurrenceId = String(slot.occurrenceId || "");
      const startTime = String(slot.startTime || "");
      const endTime = String(slot.endTime || "");
      expect(String(slot.id || "")).not.toBe("");
      expect(goalId).not.toBe("");
      expect(sourceGoalId).toBe(goalId);
      expect(occurrenceId).not.toBe("");
      expect(String(slot.planningSlotId || "")).not.toBe("");
      expect(String(slot.poolId || "")).not.toBe("");
      expect(String(slot.reasonCode || "")).not.toBe("");
      expect(Number(slot.dayNumber || 0)).toBeGreaterThan(0);
      expect(startTime).toMatch(/^(?:[01]\d|2[0-3]):[0-5]\d$/);
      expect(endTime).toMatch(/^(?:[01]\d|2[0-3]):[0-5]\d$/);
      expect(slot.timeWindow).toBe(`${startTime}-${endTime}`);
      expect(slot.groundingStatus).toBe("unresolved");
      expect(slot.simpleDirectionRequirementLineageConflict).toBe(false);
      expect(slot.futureRouteAnchor).toBe(true);
      expect(slot.routeAnchorExpected).toBe(true);
      expect(slot.poi).toBeUndefined();
      return {
        id: String(slot.id || ""),
        goalId,
        sourceGoalId,
        occurrenceId,
        planningSlotId: String(slot.planningSlotId || ""),
        poolId: String(slot.poolId || ""),
        dayNumber: Number(slot.dayNumber || 0),
        startTime,
        endTime,
        timeWindow: String(slot.timeWindow || ""),
        reasonCode: String(slot.reasonCode || ""),
        groundingStatus: String(slot.groundingStatus || ""),
      };
    });

  // The journey explicitly selected one night-view occurrence during
  // clarification. Exactly one truthful representation must survive.
  expect(materialized.length + providerExhaustedPending.length).toBe(1);
  expect(materialized.length > 0 && providerExhaustedPending.length > 0).toBe(
    false,
  );
  return {
    branch:
      materialized.length === 1 ? "materialized" : "provider_exhausted_pending",
    materialized,
    providerExhaustedPending,
  };
}

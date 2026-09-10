import {
  expect,
  test,
  type Locator,
  type Page,
  type Request,
  type Response,
} from "@playwright/test";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";

import { awaitingClarificationBatchCards } from "./support/clarification-card-selectors";
import {
  extractExactSelectedAgentChoice,
  readLatestSimpleDirectionLiveState,
  selectStrictDetourOption,
  type CapturedStreamRequest,
} from "./support/simple-direction-live-state";

const INITIAL_REQUEST =
  "今年国庆10月1日北京一日游，1人，中等预算。上午参观一所北京985高校，中午吃北京菜，下午逛公共开放的城市公园。公共交通为主，步行节奏标准，最多接受35分钟或35%绕路。";
const GUIDE_GROUNDED_REQUEST = "参考攻略建议的地点，生成新的方案";
const API_BASE =
  process.env.TRIP_E2E_API_BASE_URL || "http://localhost:8000/api";
const STREAM_PATH = /\/api\/agent\/sessions\/[^/]+\/messages\/stream(?:\?|$)/;
const SHA256_RE = /^[0-9a-f]{64}$/i;

type JsonRecord = Record<string, unknown>;

test("真实攻略地点生成新方向并通过同一 opaque choice exactly-once 采用", async ({
  context,
  page,
}, testInfo) => {
  const artifactDir = path.resolve(
    process.env.TRIP_E2E_ARTIFACT_DIR || testInfo.outputDir,
  );
  const runId = process.env.TRIP_E2E_RUN_ID || `guide-${Date.now()}`;
  const gitCommit = String(process.env.TRIP_E2E_GIT_COMMIT || "")
    .trim()
    .toLowerCase();
  expect(gitCommit).toMatch(/^[0-9a-f]{40}$/);
  await mkdir(artifactDir, { recursive: true });

  const streamResponses: Response[] = [];
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
  });

  await page.goto("/", { waitUntil: "domcontentloaded" });
  await expect(
    page.getByRole("form", { name: "Agent 对话输入" }),
  ).toBeVisible();

  const initialSessions = await fetchJson(page, `${API_BASE}/agent/sessions`);
  expect(asArray(initialSessions.sessions)).toHaveLength(0);

  const providerStatus = await fetchJson(page, `${API_BASE}/providers/status`);
  const providerAgent = asRecord(providerStatus.agent);
  const amapWeather = asRecord(asRecord(providerStatus.tools).amapWeather);
  expect(providerStatus.mode).toBe("default");
  expect(providerAgent.configured).toBe(true);
  expect(providerAgent.status).not.toBe("unavailable");
  expect(String(providerAgent.providerName || "")).toMatch(/deepseek/i);
  expect(amapWeather.configured).toBe(true);

  const mapConfig = await fetchJson(page, `${API_BASE}/map/config`);
  expect(mapConfig.enabled).toBe(true);
  expect(String(mapConfig.jsApiKey || "").length).toBeGreaterThan(8);

  // Readiness payloads may contain credentials. Retained tracing starts only
  // after preflight and intentionally excludes DOM/network snapshots.
  const tracePath = path.join(artifactDir, "trace.zip");
  await context.tracing.start({
    screenshots: false,
    snapshots: false,
    sources: true,
  });

  try {
    const textarea = page.getByRole("textbox", { name: "Agent 对话文本" });
    await textarea.fill(INITIAL_REQUEST);
    await textarea.press("Enter");
    await waitForSettledStreams(page, streamResponses, 1);
    const clarificationEvidence = await completeOptionalClarifications(
      page,
      streamResponses,
    );
    const retryEvidence = await retryInitialPlanningOnceIfRequired(
      page,
      streamResponses,
      streamRequests,
    );

    await page.getByRole("tab", { name: "行程对比" }).click();
    const offeredSession = await fetchJson(
      page,
      `${API_BASE}/agent/sessions/current`,
    );
    const sessionId = String(
      offeredSession.sessionId || offeredSession.id || "",
    );
    expect(sessionId).not.toBe("");
    expect(String(offeredSession.activeVersionId || "")).toBe("");
    const initialProposalIds = proposalIds(offeredSession);
    expect(initialProposalIds.length).toBeGreaterThanOrEqual(1);

    const initialProposalTurn = latestAssistantTurn(offeredSession);
    const ordinaryState = readLatestSimpleDirectionLiveState(offeredSession);
    expect(ordinaryState.kind).toBe("comparison");
    if (ordinaryState.kind !== "comparison" || !ordinaryState.capability) {
      throw new Error(
        `ordinary_continuation_capability_missing:${ordinaryState.kind === "comparison" ? ordinaryState.stopReason : ordinaryState.kind}`,
      );
    }
    const ordinaryChoice = exactChoice(initialProposalTurn, {
      action: "continue_plan_expansion",
      choiceId: ordinaryState.capability.choiceId,
    });
    expect(ordinaryChoice.kind).toBe("simple_direction_more_plans");
    const ordinaryPair = choicePair(initialProposalTurn, ordinaryChoice);
    const ordinaryButton = await findExactChoiceButton(
      page,
      ordinaryPair.sourceAssistantTurnId,
      "continue_plan_expansion",
      ordinaryPair.choiceId,
    );
    const ordinaryRequestStart = streamRequests.length;
    const ordinaryResponseStart = streamResponses.length;
    await ordinaryButton.click();
    await waitForSettledStreams(
      page,
      streamResponses,
      ordinaryResponseStart + 1,
    );
    const ordinaryRequests = streamRequests.slice(ordinaryRequestStart);
    expect(ordinaryRequests).toHaveLength(1);
    expect(extractExactSelectedAgentChoice(ordinaryRequests[0].body)).toEqual(
      ordinaryPair,
    );

    const ordinarySession = await fetchJson(
      page,
      `${API_BASE}/agent/sessions/current`,
    );
    expect(String(ordinarySession.activeVersionId || "")).toBe("");
    const baselineProposalIds = proposalIds(ordinarySession);
    const ordinaryProposalIds = baselineProposalIds.filter(
      (proposalId) => !initialProposalIds.includes(proposalId),
    );
    expect(ordinaryProposalIds).toHaveLength(1);
    expect(baselineProposalIds.length).toBe(initialProposalIds.length + 1);
    expect(baselineProposalIds.length).toBeGreaterThanOrEqual(2);
    const ordinaryProposalTurn = latestAssistantTurn(ordinarySession);
    expect(ordinaryProposalTurn.mode).toBe("simple_open_direction_proposal");
    expect(Number(ordinaryProposalTurn.proposalDelta || 0)).toBe(1);
    for (const key of ["versionDelta", "patchDelta", "routeWriteDelta"]) {
      expect(Number(ordinaryProposalTurn[key] || 0)).toBe(0);
    }

    const guideSourceTurn = ordinaryProposalTurn;
    const guideChoice = exactChoice(guideSourceTurn, {
      action: "search_travel_guide_advice",
    });
    const guidePair = choicePair(guideSourceTurn, guideChoice);
    const guideButton = await findExactChoiceButton(
      page,
      guidePair.sourceAssistantTurnId,
      "search_travel_guide_advice",
      guidePair.choiceId,
    );
    const guideRequestStart = streamRequests.length;
    const guideResponseStart = streamResponses.length;
    await guideButton.click();
    await waitForSettledStreams(
      page,
      streamResponses,
      guideResponseStart + 1,
    );
    const guideRequests = streamRequests.slice(guideRequestStart);
    expect(guideRequests).toHaveLength(1);
    expect(extractExactSelectedAgentChoice(guideRequests[0].body)).toEqual(
      guidePair,
    );

    const guideSession = await fetchJson(
      page,
      `${API_BASE}/agent/sessions/current`,
    );
    expect(String(guideSession.activeVersionId || "")).toBe("");
    expect(proposalIds(guideSession)).toEqual(baselineProposalIds);
    const guideResultTurn = latestAssistantTurn(guideSession);
    expect(guideResultTurn.mode).toBe("travel_guide_advice");
    const guideAdvice = asRecord(guideResultTurn.guideAdvice);
    expect(guideAdvice.status).toBe("completed");
    expect(String(guideAdvice.queryFingerprint || "")).toMatch(SHA256_RE);
    expect(String(guideAdvice.evidenceFingerprint || "")).toMatch(SHA256_RE);
    expect(asArray(guideAdvice.sourceRefs).length).toBeGreaterThanOrEqual(1);
    expect(asArray(guideAdvice.placeHints).length).toBeGreaterThanOrEqual(1);

    const continuationState = readLatestSimpleDirectionLiveState(guideSession);
    expect(continuationState.kind).toBe("comparison");
    if (continuationState.kind !== "comparison") {
      throw new Error("guide_continuation_state_not_comparison");
    }
    expect(continuationState.capability).not.toBeNull();
    if (!continuationState.capability) {
      throw new Error(
        `guide_continuation_capability_missing:${continuationState.stopReason}`,
      );
    }
    const continuationPair = {
      sourceAssistantTurnId: String(
        continuationState.capability.sourceAssistantTurnId || "",
      ),
      choiceId: String(continuationState.capability.choiceId || ""),
    };
    const continuationChoice = exactChoice(guideResultTurn, {
      action: "continue_plan_expansion",
      choiceId: continuationPair.choiceId,
    });
    expect(
      String(continuationChoice.guideEvidenceSourceAssistantTurnId || ""),
    ).toBe(String(guideResultTurn.id || ""));
    expect(String(continuationChoice.guideEvidenceFingerprint || "")).toBe(
      String(guideAdvice.evidenceFingerprint || ""),
    );

    const continuationRequestStart = streamRequests.length;
    const continuationResponseStart = streamResponses.length;
    await textarea.fill(GUIDE_GROUNDED_REQUEST);
    await textarea.press("Enter");
    await waitForSettledStreams(
      page,
      streamResponses,
      continuationResponseStart + 1,
    );
    const continuationRequests = streamRequests.slice(
      continuationRequestStart,
    );
    expect(continuationRequests).toHaveLength(1);
    expect(String(continuationRequests[0].body.message || "")).toBe(
      GUIDE_GROUNDED_REQUEST,
    );
    expect(
      extractExactSelectedAgentChoice(continuationRequests[0].body),
    ).toEqual(continuationPair);

    const proposedSession = await fetchJson(
      page,
      `${API_BASE}/agent/sessions/current`,
    );
    expect(String(proposedSession.activeVersionId || "")).toBe("");
    const proposalTurn = latestAssistantTurn(proposedSession);
    expect(proposalTurn.mode).toBe("simple_open_direction_proposal");
    expect(proposalTurn.workflowMode).toBe("simple_direction_v1");
    expect(Number(proposalTurn.proposalDelta || 0)).toBe(1);
    for (const key of ["versionDelta", "patchDelta", "routeWriteDelta"]) {
      expect(Number(proposalTurn[key] || 0)).toBe(0);
    }
    const guideUsage = asRecord(proposalTurn.guideEvidenceUsage);
    assertSatisfiedGuideUsage(guideUsage);
    const proposalStopSteps = asArray(proposalTurn.planningSteps)
      .map(asRecord)
      .filter((step) => step.type === "agent_stop");
    expect(proposalStopSteps).toHaveLength(1);
    const proposalControllerMetrics = asRecord(
      proposalStopSteps[0].metadata,
    );
    expect(Number(proposalControllerMetrics.controllerFullCallCount || 0)).toBe(
      1,
    );
    expect(Number(proposalControllerMetrics.controllerLiteCallCount || 0)).toBe(
      0,
    );
    expect(proposalControllerMetrics.plannerCalled).toBe(true);

    const afterProposalIds = proposalIds(proposedSession);
    const newProposalIds = afterProposalIds.filter(
      (proposalId) => !baselineProposalIds.includes(proposalId),
    );
    expect(newProposalIds).toHaveLength(1);
    const proposalId = newProposalIds[0];
    const proposalProjection = findProjection(proposedSession, proposalId);
    expect(proposalProjection.adoptionReady).toBe(true);
    assertSatisfiedGuideUsage(
      asRecord(proposalProjection.guideEvidenceUsage),
    );

    await page.getByRole("tab", { name: "行程对比" }).click();
    const proposalCard = page.locator(
      `article[data-proposal-id="${proposalId}"][data-adoption-ready="true"]`,
    );
    await expect(proposalCard).toHaveCount(1);

    const selectionChoice = exactChoice(proposalTurn, {
      action: "select_plan_proposal",
      proposalId,
    });
    const selectionPair = choicePair(proposalTurn, selectionChoice);
    const selectionButton = await findExactChoiceButton(
      page,
      selectionPair.sourceAssistantTurnId,
      "select_plan_proposal",
      selectionPair.choiceId,
    );
    await expect(selectionButton).toBeVisible();
    await expect(selectionButton).toBeEnabled();

    const selectionRequestStart = streamRequests.length;
    const selectionResponseStart = streamResponses.length;
    await selectionButton.click();
    await waitForSettledStreams(
      page,
      streamResponses,
      selectionResponseStart + 1,
    );
    const selectionRequests = streamRequests.slice(selectionRequestStart);
    expect(selectionRequests).toHaveLength(1);
    expect(extractExactSelectedAgentChoice(selectionRequests[0].body)).toEqual(
      selectionPair,
    );

    const adoptedSession = await fetchJson(
      page,
      `${API_BASE}/agent/sessions/current`,
    );
    const activeVersionId = String(adoptedSession.activeVersionId || "");
    expect(activeVersionId).not.toBe("");
    const adoptionTurn = latestAssistantTurn(adoptedSession);
    expect(String(adoptionTurn.activeVersionId || "")).toBe(activeVersionId);
    expect(String(adoptionTurn.selectedProposalId || proposalId)).toBe(
      proposalId,
    );
    expect(Number(adoptionTurn.versionDelta || 0)).toBe(1);
    expect(Number(adoptionTurn.patchDelta || 0)).toBe(1);
    expect(Number(adoptionTurn.routeWriteDelta || 0)).toBeGreaterThanOrEqual(1);

    await page.reload({ waitUntil: "domcontentloaded" });
    await expect(
      page.getByRole("form", { name: "Agent 对话输入" }),
    ).toBeVisible();
    const reloadedSession = await fetchJson(
      page,
      `${API_BASE}/agent/sessions/${sessionId}`,
    );
    expect(String(reloadedSession.activeVersionId || "")).toBe(
      activeVersionId,
    );

    const result = {
      schemaVersion: "trip-simple-direction-guide-grounded-live-v1",
      runId,
      gitCommit,
      sessionId,
      initialRequest: INITIAL_REQUEST,
      guideGroundedRequest: GUIDE_GROUNDED_REQUEST,
      baselineProposalIds,
      proposalId,
      activeVersionId,
      providerReadiness: {
        mode: providerStatus.mode,
        agentProviderName: String(providerAgent.providerName || ""),
        agentConfigured: providerAgent.configured,
        amapConfigured: amapWeather.configured,
        browserMapEnabled: mapConfig.enabled,
      },
      clarificationEvidence,
      retryEvidence,
      opaqueChoices: {
        ordinaryContinuation: ordinaryPair,
        guideSearch: guidePair,
        guideContinuation: continuationPair,
        proposalSelection: selectionPair,
      },
      ordinaryContinuationEvidence: {
        sourceAssistantTurnId: String(initialProposalTurn.id || ""),
        assistantTurnId: String(ordinaryProposalTurn.id || ""),
        initialProposalIds,
        newProposalIds: ordinaryProposalIds,
        proposalDelta: Number(ordinaryProposalTurn.proposalDelta || 0),
        versionDelta: Number(ordinaryProposalTurn.versionDelta || 0),
        patchDelta: Number(ordinaryProposalTurn.patchDelta || 0),
        routeWriteDelta: Number(ordinaryProposalTurn.routeWriteDelta || 0),
      },
      guideEvidence: {
        sourceAssistantTurnId: String(guideResultTurn.id || ""),
        queryFingerprint: String(guideAdvice.queryFingerprint || ""),
        evidenceFingerprint: String(guideAdvice.evidenceFingerprint || ""),
        sourceCount: asArray(guideAdvice.sourceRefs).length,
        placeHintCount: asArray(guideAdvice.placeHints).length,
      },
      proposalEvidence: {
        assistantTurnId: String(proposalTurn.id || ""),
        proposalDelta: Number(proposalTurn.proposalDelta || 0),
        versionDelta: Number(proposalTurn.versionDelta || 0),
        patchDelta: Number(proposalTurn.patchDelta || 0),
        routeWriteDelta: Number(proposalTurn.routeWriteDelta || 0),
        controllerFullCallCount: Number(
          proposalControllerMetrics.controllerFullCallCount || 0,
        ),
        controllerLiteCallCount: Number(
          proposalControllerMetrics.controllerLiteCallCount || 0,
        ),
        plannerCalled: proposalControllerMetrics.plannerCalled === true,
        guideEvidenceUsage: guideUsage,
      },
      adoptionEvidence: {
        assistantTurnId: String(adoptionTurn.id || ""),
        activeVersionId,
        selectedProposalId: proposalId,
        versionDelta: Number(adoptionTurn.versionDelta || 0),
        patchDelta: Number(adoptionTurn.patchDelta || 0),
        routeWriteDelta: Number(adoptionTurn.routeWriteDelta || 0),
        reloadPreservedActiveVersion: true,
      },
      streamRequestCount: streamRequests.length,
      streamResponseCount: streamResponses.length,
    };
    await writeFile(
      path.join(artifactDir, "journey-result.json"),
      JSON.stringify(result, null, 2),
      "utf8",
    );
    await page.screenshot({
      path: path.join(artifactDir, "guide-grounded-adopted-itinerary.png"),
      fullPage: true,
    });
  } finally {
    await context.tracing.stop({ path: tracePath });
  }
});

function assertSatisfiedGuideUsage(usage: JsonRecord) {
  expect(usage.schemaVersion).toBe("guide-evidence-usage-v1");
  expect(usage.status).toBe("satisfied");
  expect(String(usage.evidenceFingerprint || "")).toMatch(SHA256_RE);
  expect(String(usage.requirementFingerprint || "")).toMatch(SHA256_RE);
  const requiredMinimum = Number(usage.requiredMinimum || 0);
  const usedPlaces = asArray(usage.usedPlaces).map(asRecord);
  expect(requiredMinimum).toBeGreaterThanOrEqual(1);
  expect(usedPlaces.length).toBeGreaterThanOrEqual(requiredMinimum);
  expect(
    usedPlaces.some(
      (place) =>
        place.routeVerified === true &&
        String(place.amapPoiId || "").length > 0,
    ),
  ).toBe(true);
}

async function completeOptionalClarifications(
  page: Page,
  responses: Response[],
) {
  const evidence: JsonRecord[] = [];
  for (let batch = 0; batch < 3; batch += 1) {
    const card = awaitingClarificationBatchCards(page);
    if ((await card.count()) === 0) break;
    await expect(card).toHaveCount(1);
    const session = await fetchJson(
      page,
      `${API_BASE}/agent/sessions/current`,
    );
    expect(String(session.activeVersionId || "")).toBe("");
    const checkpoint = latestAwaitingCheckpoint(session);
    const questions = asArray(checkpoint.questions).map(asRecord);
    expect(questions.length).toBeGreaterThanOrEqual(1);
    const selections: JsonRecord[] = [];
    for (const question of questions) {
      const dimensionId = String(question.dimensionId || "");
      const questionText = String(question.question || "");
      const options = asArray(question.options).map(asRecord);
      expect(dimensionId).not.toBe("");
      expect(questionText).not.toBe("");
      expect(options.length).toBeGreaterThanOrEqual(2);
      const selected =
        dimensionId === "route_decision.detour_tolerance"
          ? selectStrictDetourOption(question)
          : {
              optionId: String(options[0].id || ""),
              semanticValue: options[0].semanticValue,
              submissionMode: "persisted_option",
            };
      expect(selected.optionId).not.toBe("");
      const group = card.getByRole("group", {
        name: questionText,
        exact: true,
      });
      await group
        .locator(
          `input[type="radio"][data-option-id="${selected.optionId}"]`,
        )
        .check();
      selections.push({ dimensionId, ...selected });
    }
    const responseStart = responses.length;
    await card
      .getByRole("button", { name: "确认并开始规划", exact: true })
      .click();
    await waitForSettledStreams(page, responses, responseStart + 1);
    evidence.push({
      checkpointId: String(checkpoint.checkpointId || ""),
      sourceAssistantTurnId: String(
        checkpoint.sourceAssistantTurnId || "",
      ),
      selections,
    });
  }
  expect(await awaitingClarificationBatchCards(page).count()).toBe(0);
  return evidence;
}

async function retryInitialPlanningOnceIfRequired(
  page: Page,
  responses: Response[],
  requests: CapturedStreamRequest[],
) {
  const session = await fetchJson(page, `${API_BASE}/agent/sessions/current`);
  const state = readLatestSimpleDirectionLiveState(session);
  if (state.kind !== "retry_model_planning") return null;
  const button = await findExactChoiceButton(
    page,
    state.capability.sourceAssistantTurnId,
    "retry_model_planning",
    state.capability.choiceId,
  );
  const requestStart = requests.length;
  const responseStart = responses.length;
  await button.click();
  await waitForSettledStreams(page, responses, responseStart + 1);
  const captured = requests.slice(requestStart);
  expect(captured).toHaveLength(1);
  expect(extractExactSelectedAgentChoice(captured[0].body)).toEqual(
    state.capability,
  );
  const after = readLatestSimpleDirectionLiveState(
    await fetchJson(page, `${API_BASE}/agent/sessions/current`),
  );
  expect(after.kind).toBe("comparison");
  return {
    ...state.capability,
    streamRequestDelta: 1,
    streamResponseDelta: 1,
  };
}

function latestAwaitingCheckpoint(session: JsonRecord): JsonRecord {
  const turns = asArray(session.turns).map(asRecord).reverse();
  const turn = turns.find(
    (candidate) =>
      asRecord(candidate.clarificationCheckpoint).status ===
      "awaiting_answer",
  );
  if (!turn) throw new Error("awaiting_clarification_checkpoint_missing");
  return asRecord(turn.clarificationCheckpoint);
}

function latestAssistantTurn(session: JsonRecord): JsonRecord {
  const turns = asArray(session.turns).map(asRecord);
  const turn = [...turns].reverse().find((candidate) => candidate.role === "assistant");
  if (!turn) throw new Error("latest_assistant_turn_missing");
  expect(String(turn.id || "")).not.toBe("");
  return turn;
}

function exactChoice(
  turn: JsonRecord,
  criteria: { action: string; choiceId?: string; proposalId?: string },
): JsonRecord {
  const matches = asArray(turn.choiceOptions)
    .map(asRecord)
    .filter(
      (choice) =>
        choice.action === criteria.action &&
        (!criteria.choiceId || choice.id === criteria.choiceId) &&
        (!criteria.proposalId || choice.proposalId === criteria.proposalId),
    );
  expect(matches).toHaveLength(1);
  return matches[0];
}

function choicePair(turn: JsonRecord, choice: JsonRecord) {
  const pair = {
    sourceAssistantTurnId: String(choice.sourceAssistantTurnId || ""),
    choiceId: String(choice.id || choice.choiceId || ""),
  };
  expect(pair.sourceAssistantTurnId).toBe(String(turn.id || ""));
  expect(pair.choiceId).not.toBe("");
  expect(String(choice.choiceId || pair.choiceId)).toBe(pair.choiceId);
  return pair;
}

async function findExactChoiceButton(
  page: Page,
  sourceAssistantTurnId: string,
  action: string,
  choiceId: string,
): Promise<Locator> {
  const group = page.locator(
    `[data-assistant-response-group="${sourceAssistantTurnId}"]`,
  );
  await expect(group).toHaveCount(1);
  const button = group.locator(
    `button[data-choice-action="${action}"][data-choice-id="${choiceId}"]`,
  );
  await expect(button).toHaveCount(1);
  await expect(button).toBeVisible();
  await expect(button).toBeEnabled();
  return button;
}

function proposalIds(session: JsonRecord): string[] {
  const ids = new Set<string>();
  for (const turn of asArray(session.turns).map(asRecord)) {
    for (const projection of asArray(turn.comparisonProjections).map(asRecord)) {
      const proposalId = String(projection.proposalId || projection.id || "");
      if (proposalId) ids.add(proposalId);
    }
  }
  return [...ids].sort();
}

function findProjection(session: JsonRecord, proposalId: string): JsonRecord {
  for (const turn of asArray(session.turns).map(asRecord).reverse()) {
    const projection = asArray(turn.comparisonProjections)
      .map(asRecord)
      .find(
        (candidate) =>
          String(candidate.proposalId || candidate.id || "") === proposalId,
      );
    if (projection) return projection;
  }
  throw new Error(`proposal_projection_missing:${proposalId}`);
}

async function waitForSettledStreams(
  page: Page,
  responses: Response[],
  minimumCount: number,
) {
  await expect
    .poll(() => responses.length, { timeout: 120_000 })
    .toBeGreaterThanOrEqual(minimumCount);
  let stablePasses = 0;
  let lastCount = -1;
  while (stablePasses < 3) {
    const current = [...responses];
    const errors = await Promise.all(current.map((response) => response.finished()));
    expect(errors.filter(Boolean)).toEqual([]);
    for (const response of current) expect(response.status()).toBe(200);
    await page.waitForTimeout(800);
    const enabled = await page
      .getByRole("textbox", { name: "Agent 对话文本" })
      .isEnabled();
    if (
      responses.length === current.length &&
      responses.length === lastCount &&
      enabled
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

function asRecord(value: unknown): JsonRecord {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as JsonRecord)
    : {};
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

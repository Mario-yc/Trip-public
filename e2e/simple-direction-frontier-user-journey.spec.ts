import {
  expect,
  test,
  type Page,
  type Request,
  type Response,
} from "@playwright/test";
import { createHash } from "node:crypto";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";

import { awaitingClarificationBatchCards } from "./support/clarification-card-selectors";

const USER_REQUEST =
  "2026年10月1日至2日，1人，中等预算，全程公共交通，每天09:00至19:00规划北京两日985高校游。每天严格按“北京985高校→12:00至13:30北京当地特色午餐→独立公共公园”的顺序安排3个不同地点；两天的高校、餐厅和公园均不得重复。高校必须是北京985，排除北京科技大学等非985学校；午餐必须为高德分类中的北京菜，不接受高校食堂或泛餐饮；公园必须是独立公共公园，不得用校园景观代替。地点间尽量少绕路，只有每天2段相邻公共交通路线都经高德核验后，方案才可确认编辑。";
const MANUAL_DETOUR_PREFERENCE = "尽量少绕路，但景点间的距离不要间隔太远了";
const API_BASE =
  process.env.TRIP_E2E_API_BASE_URL || "http://localhost:8000/api";
const STREAM_PATH = /\/api\/agent\/sessions\/[^/]+\/messages\/stream(?:\?|$)/;
const MAX_CONTINUATION_ROUNDS = 3;
const MAX_CLARIFICATION_BATCHES = 6;

type JsonRecord = Record<string, unknown>;

type CapturedStreamRequest = {
  body: JsonRecord;
  rawBody: string;
  url: string;
};

type ComparisonState = {
  comparisonSummary: JsonRecord;
  continueChoice: JsonRecord | null;
  planningRootId: string;
  portfolioId: string;
  projections: JsonRecord[];
  ready: JsonRecord[];
  partial: JsonRecord[];
  sourceAssistantTurnId: string;
};

test("真实 985 高校前沿持续探索、独立体验门禁与采用幂等闭环", async ({
  context,
  page,
}, testInfo) => {
  const artifactDir = path.resolve(
    process.env.TRIP_E2E_ARTIFACT_DIR || testInfo.outputDir,
  );
  const runId = process.env.TRIP_E2E_RUN_ID || `frontier-${Date.now()}`;
  const gitCommit = String(process.env.TRIP_E2E_GIT_COMMIT || "")
    .trim()
    .toLowerCase();
  expect(gitCommit).toMatch(/^[0-9a-f]{40}$/);
  const tracePath = path.join(artifactDir, "trace.zip");
  const preAdoptionBundlePath = path.join(
    artifactDir,
    "pre-adoption-debug-bundle.json",
  );
  const postAdoptionBundlePath = path.join(
    artifactDir,
    "post-adoption-debug-bundle.json",
  );
  const finalBundlePath = path.join(artifactDir, "debug-bundle.json");
  const adoptionAttemptPath = path.join(artifactDir, "adoption-attempt.json");
  const resultPath = path.join(artifactDir, "journey-result.json");
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

  const initialSessionList = await fetchJson(
    page,
    `${API_BASE}/agent/sessions`,
  );
  expect(asArray(initialSessionList.sessions)).toHaveLength(0);

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

  // Provider and map configuration responses may contain credentials. Keep
  // them out of the retained trace by starting tracing only after validation.
  await context.tracing.start({
    screenshots: false,
    snapshots: false,
    sources: true,
  });

  let resultWritten = false;
  let diagnosticSessionId = "";
  let adoptionAttemptStarted = false;
  let adoptionAttemptWritten = false;
  let adoptionAttemptWriteError: string | null = null;
  try {
    const textarea = page.getByRole("textbox", { name: "Agent 对话文本" });
    await textarea.fill(USER_REQUEST);
    await textarea.press("Enter");
    await waitForSettledStreams(page, streamResponses, 1);

    const clarification = await completeClarificationBatches(
      page,
      streamResponses,
    );
    expect(clarification.batches.length).toBeGreaterThanOrEqual(1);

    const createdSession = await fetchJson(
      page,
      `${API_BASE}/agent/sessions/current`,
    );
    const sessionId = String(
      createdSession.sessionId || createdSession.id || "",
    );
    expect(sessionId).not.toBe("");
    diagnosticSessionId = sessionId;
    expect(String(createdSession.activeVersionId || "")).toBe("");

    await page.getByRole("tab", { name: "行程对比" }).click();

    const rounds: JsonRecord[] = [];
    const seenProposalIds = new Set<string>();
    let continuationClicks = 0;
    let stopReason = "max_continuation_rounds_reached";

    for (
      let roundIndex = 0;
      roundIndex <= MAX_CONTINUATION_ROUNDS;
      roundIndex += 1
    ) {
      const session = await fetchJson(
        page,
        `${API_BASE}/agent/sessions/${sessionId}`,
      );
      expect(String(session.activeVersionId || "")).toBe("");
      const state = currentComparisonState(session);
      const ui = await assertComparisonUi(page, state);
      const proposalIds = state.projections.map((projection) =>
        String(projection.proposalId || ""),
      );
      const newProposalIds = proposalIds.filter(
        (proposalId) => proposalId && !seenProposalIds.has(proposalId),
      );
      proposalIds.forEach((proposalId) => seenProposalIds.add(proposalId));
      rounds.push({
        roundIndex,
        activeVersionId: session.activeVersionId ?? null,
        planningRootId: state.planningRootId,
        portfolioId: state.portfolioId,
        sourceAssistantTurnId: state.sourceAssistantTurnId,
        comparisonSummary: state.comparisonSummary,
        proposalIds,
        newProposalIds,
        readyProposalIds: state.ready.map((projection) =>
          String(projection.proposalId || ""),
        ),
        partialProposalIds: state.partial.map((projection) =>
          String(projection.proposalId || ""),
        ),
        readyTitles: state.ready.map((projection) =>
          String(projection.displayTitle || projection.title || ""),
        ),
        ui,
        continueCapability: state.continueChoice
          ? choiceIdentity(state.continueChoice, state.sourceAssistantTurnId)
          : null,
      });

      if (state.ready.length >= 3) {
        stopReason = "three_ready_proposals_observed";
        break;
      }
      if (roundIndex === MAX_CONTINUATION_ROUNDS) {
        stopReason = "max_continuation_rounds_reached";
        break;
      }

      const frontierStatus = String(
        state.comparisonSummary.frontierStatus || "",
      );
      if (frontierStatus !== "has_more") {
        stopReason = `frontier_${frontierStatus || "status_missing"}`;
        break;
      }
      if (!state.continueChoice) {
        stopReason =
          state.ready.length === 0
            ? "no_ready_proposal_for_continuation"
            : "continue_capability_missing_while_frontier_has_more";
        if (state.ready.length > 0) {
          throw new Error(stopReason);
        }
        break;
      }

      const continueChoiceId = String(
        state.continueChoice.id || state.continueChoice.choiceId || "",
      );
      expect(continueChoiceId).not.toBe("");
      const continueButton = page
        .locator(
          `button[data-choice-action="continue_plan_expansion"][data-choice-id="${continueChoiceId}"]`,
        )
        .last();
      await expect(continueButton).toBeVisible();
      await expect(continueButton).toBeEnabled();
      const streamsBefore = streamResponses.length;
      await continueButton.click();
      await waitForSettledStreams(page, streamResponses, streamsBefore + 1);
      expect(streamResponses.length).toBe(streamsBefore + 1);
      continuationClicks += 1;
      rounds[rounds.length - 1].continueClicked = true;
      await page.getByRole("tab", { name: "行程对比" }).click();
    }

    const preAdoptionSession = await fetchJson(
      page,
      `${API_BASE}/agent/sessions/${sessionId}`,
    );
    expect(String(preAdoptionSession.activeVersionId || "")).toBe("");
    const preAdoptionState = currentComparisonState(preAdoptionSession);
    const preAdoptionBundle = await fetchJson(
      page,
      `${API_BASE}/agent/sessions/${sessionId}/debug-bundle`,
    );
    await writeJson(preAdoptionBundlePath, preAdoptionBundle);
    const preAdoptionCounts = debugBundleCounts(preAdoptionBundle);
    expect(preAdoptionCounts.itineraryVersionCount).toBe(0);
    expect(preAdoptionCounts.patchCount).toBe(0);
    expect(preAdoptionCounts.formalRouteWriteCount).toBe(0);

    const frontierEvidence = currentFrontierEvidence(
      preAdoptionBundle,
      preAdoptionState.portfolioId,
    );
    const proposalEvidence = proposalEvidenceFromBundle(
      preAdoptionBundle,
      preAdoptionState.portfolioId,
    );
    assertReadyProposalEvidence(
      preAdoptionState,
      proposalEvidence,
      frontierEvidence,
    );
    assertSiblingTitleShapeDifference(
      preAdoptionState.ready.map((projection) =>
        String(projection.displayTitle || projection.title || ""),
      ),
    );
    expect(preAdoptionState.ready.length).toBeGreaterThan(0);

    let adoption: JsonRecord = {
      status: "not_attempted_no_ready_proposal",
      proposalId: null,
      firstActiveVersionId: null,
      replayActiveVersionId: null,
    };
    let finalBundle = preAdoptionBundle;

    if (preAdoptionState.ready.length > 0) {
      const selectedProjection = preAdoptionState.ready[0];
      const proposalId = String(selectedProjection.proposalId || "");
      const sourceAssistantTurnId = String(
        selectedProjection.sourceAssistantTurnId || "",
      );
      const choiceId = String(selectedProjection.choiceId || "");
      expect(proposalId).not.toBe("");
      expect(sourceAssistantTurnId).not.toBe("");
      expect(choiceId).not.toBe("");

      const requestOffset = streamRequests.length;
      const responseOffset = streamResponses.length;
      const selectedCard = page.locator(
        `article[data-proposal-id="${proposalId}"][data-section="ready"]`,
      );
      const confirmButton = selectedCard.getByRole("button", {
        name: /^确认编辑「.+」$/,
      });
      await expect(confirmButton).toBeVisible();
      await expect(confirmButton).toBeEnabled();
      const adoptionPreflight = await waitForSessionIdle(page, sessionId);
      adoptionAttemptStarted = true;
      let capturedAdoptionRequest: CapturedStreamRequest | undefined;
      let adoptionOperationFailed = false;
      let adoptionOperationError: unknown;
      try {
        await confirmButton.click();
        await waitForSettledStreams(page, streamResponses, responseOffset + 1);
      } catch (error) {
        adoptionOperationFailed = true;
        adoptionOperationError = error;
      } finally {
        const requestsSinceClick = streamRequests.slice(requestOffset);
        const responsesSinceClick = streamResponses.slice(responseOffset);
        capturedAdoptionRequest = findChoiceRequest(
          requestsSinceClick,
          sourceAssistantTurnId,
          choiceId,
        );
        try {
          await writeJson(
            adoptionAttemptPath,
            await adoptionAttemptEvidence({
              runId,
              sessionId,
              proposalId,
              expectedChoice: {
                sourceAssistantTurnId,
                choiceId,
                planningSelectionRootTurnId: String(
                  selectedProjection.planningSelectionRootTurnId || "",
                ),
                rootPortfolioId: String(
                  selectedProjection.rootPortfolioId || "",
                ),
                requestContractFingerprint: String(
                  selectedProjection.requestContractFingerprint || "",
                ),
              },
              preflight: adoptionPreflight,
              capturedRequest: capturedAdoptionRequest,
              requestsSinceClick,
              responsesSinceClick,
            }),
          );
          adoptionAttemptWritten = true;
        } catch (error) {
          adoptionAttemptWriteError =
            error instanceof Error ? error.name : "Error";
        }
      }
      if (adoptionOperationFailed) {
        throw adoptionOperationError;
      }
      if (adoptionAttemptWriteError) {
        throw new Error(
          `adoption_attempt_artifact_write_failed:${adoptionAttemptWriteError}`,
        );
      }
      expect(streamResponses.length).toBe(responseOffset + 1);
      expect(capturedAdoptionRequest).toBeTruthy();
      if (!capturedAdoptionRequest) {
        throw new Error("adoption_request_payload_not_captured");
      }

      const postAdoptionSession = await fetchJson(
        page,
        `${API_BASE}/agent/sessions/${sessionId}`,
      );
      const firstActiveVersionId = String(
        postAdoptionSession.activeVersionId || "",
      );
      expect(firstActiveVersionId).not.toBe("");
      const postAdoptionBundle = await fetchJson(
        page,
        `${API_BASE}/agent/sessions/${sessionId}/debug-bundle`,
      );
      await writeJson(postAdoptionBundlePath, postAdoptionBundle);
      const postAdoptionCounts = debugBundleCounts(postAdoptionBundle);

      // Replay the exact serialized browser payload once. This is an
      // idempotency probe, not a Provider retry: the persisted choice execution
      // must be returned without advancing the frontier or writing a new plan.
      const replayResponse = await page.request.post(
        capturedAdoptionRequest.url,
        {
          data: capturedAdoptionRequest.rawBody,
          headers: { "Content-Type": "application/json" },
          timeout: 10 * 60 * 1000,
        },
      );
      expect(replayResponse.status()).toBe(200);
      const replayBody = await replayResponse.text();
      const replayEvents = summarizeNdjson(replayBody);
      expect(replayEvents.eventNames).toContain("message_response");

      const replaySession = await fetchJson(
        page,
        `${API_BASE}/agent/sessions/${sessionId}`,
      );
      const replayActiveVersionId = String(replaySession.activeVersionId || "");
      expect(replayActiveVersionId).toBe(firstActiveVersionId);
      finalBundle = await fetchJson(
        page,
        `${API_BASE}/agent/sessions/${sessionId}/debug-bundle`,
      );
      await writeJson(finalBundlePath, finalBundle);
      const finalCounts = debugBundleCounts(finalBundle);
      expect(finalCounts.itineraryVersionCount).toBe(
        postAdoptionCounts.itineraryVersionCount,
      );
      expect(finalCounts.patchCount).toBe(postAdoptionCounts.patchCount);
      expect(finalCounts.formalRouteWriteCount).toBe(
        postAdoptionCounts.formalRouteWriteCount,
      );
      expect(finalCounts.choiceExecutionCount).toBe(
        postAdoptionCounts.choiceExecutionCount,
      );

      adoption = {
        status: "confirmed_and_exactly_once_replayed",
        proposalId,
        sourceAssistantTurnId,
        choiceId,
        request: {
          urlPath: new URL(capturedAdoptionRequest.url).pathname,
          bodySha256: sha256(capturedAdoptionRequest.rawBody),
          contentSha256: sha256(
            String(capturedAdoptionRequest.body.content || ""),
          ),
          contentUtf8Bytes: Buffer.byteLength(
            String(capturedAdoptionRequest.body.content || ""),
            "utf8",
          ),
          selectedAgentChoice: selectedChoiceFromPayload(
            capturedAdoptionRequest.body,
          ),
        },
        firstActiveVersionId,
        replayActiveVersionId,
        replay: {
          httpStatus: replayResponse.status(),
          bodySha256: sha256(replayBody),
          ...replayEvents,
        },
        postAdoptionCounts,
        finalCounts,
      };
    } else {
      await writeJson(finalBundlePath, finalBundle);
    }

    const finalFrontierStatus = String(
      preAdoptionState.comparisonSummary.frontierStatus || "",
    );
    const frontierConverged =
      preAdoptionState.ready.length >= 3 ||
      [
        "qualification_exhausted",
        "poi_exhausted",
        "route_feasible_exhausted",
      ].includes(finalFrontierStatus);
    const result = {
      schemaVersion: "simple-direction-frontier-live-journey-v1",
      status:
        adoption.status === "confirmed_and_exactly_once_replayed" &&
        frontierConverged
          ? "success"
          : "journey_failed_before_contract_completion",
      runId,
      gitCommit,
      sessionId,
      userRequest: USER_REQUEST,
      boundedExecution: {
        providerRetryCount: 0,
        maxContinuationRounds: MAX_CONTINUATION_ROUNDS,
        continuationClicks,
        stopReason,
        frontierConverged,
      },
      providerReadiness: {
        mode: providerStatus.mode,
        agentProviderName: String(agent.providerName || ""),
        agentStatus: String(agent.status || ""),
        amapConfigured: amapWeather.configured === true,
        mapEnabled: mapConfig.enabled === true,
      },
      clarification,
      exploration: {
        rounds,
        finalComparisonSummary: preAdoptionState.comparisonSummary,
        finalPlanningRootId: preAdoptionState.planningRootId,
        finalPortfolioId: preAdoptionState.portfolioId,
        readyProposalIds: preAdoptionState.ready.map((projection) =>
          String(projection.proposalId || ""),
        ),
        partialProposalIds: preAdoptionState.partial.map((projection) =>
          String(projection.proposalId || ""),
        ),
        frontierEvidence,
      },
      preAdoption: {
        activeVersionId: preAdoptionSession.activeVersionId ?? null,
        counts: preAdoptionCounts,
        zeroFormalWrites:
          preAdoptionCounts.itineraryVersionCount === 0 &&
          preAdoptionCounts.patchCount === 0 &&
          preAdoptionCounts.formalRouteWriteCount === 0,
      },
      proposals: proposalEvidence,
      adoption,
      artifacts: {
        playwrightTrace: "trace.zip",
        preAdoptionDebugBundle: "pre-adoption-debug-bundle.json",
        postAdoptionDebugBundle:
          adoption.status === "confirmed_and_exactly_once_replayed"
            ? "post-adoption-debug-bundle.json"
            : null,
        finalDebugBundle: "debug-bundle.json",
        adoptionAttempt: adoptionAttemptWritten
          ? "adoption-attempt.json"
          : null,
      },
    };
    await writeJson(resultPath, result);
    resultWritten = true;
    await page.screenshot({
      path: path.join(artifactDir, "frontier-final-state.png"),
      fullPage: true,
    });
  } catch (error) {
    // Keep a truthful, machine-readable artifact even when a real Provider or
    // a contract invariant stops the journey before the normal result write.
    // The launcher/verifier can then distinguish degraded external state from
    // a successful acceptance instead of relying only on Playwright stderr.
    if (!resultWritten) {
      let failureBundle: JsonRecord | null = null;
      try {
        if (!diagnosticSessionId) {
          const current = await fetchJson(
            page,
            `${API_BASE}/agent/sessions/current`,
          );
          diagnosticSessionId = String(current.sessionId || current.id || "");
        }
        if (diagnosticSessionId) {
          failureBundle = await fetchJson(
            page,
            `${API_BASE}/agent/sessions/${diagnosticSessionId}/debug-bundle`,
          );
          await writeJson(finalBundlePath, failureBundle);
        }
      } catch {
        failureBundle = null;
      }
      await writeJson(resultPath, {
        schemaVersion: "simple-direction-frontier-live-journey-v1",
        runId,
        gitCommit,
        sessionId: diagnosticSessionId || null,
        userRequest: USER_REQUEST,
        status: "journey_failed_before_contract_completion",
        failure: {
          name: error instanceof Error ? error.name : "Error",
          message:
            error instanceof Error ? error.message : "unknown journey failure",
          adoptionAttemptStarted,
          adoptionAttemptWriteError,
        },
        debugBundleCaptured: failureBundle !== null,
        artifacts: {
          playwrightTrace: "trace.zip",
          finalDebugBundle: failureBundle ? "debug-bundle.json" : null,
          adoptionAttempt: adoptionAttemptWritten
            ? "adoption-attempt.json"
            : null,
        },
      });
    }
    throw error;
  } finally {
    await context.tracing.stop({ path: tracePath });
  }
});

async function completeClarificationBatches(page: Page, responses: Response[]) {
  await expect
    .poll(() => awaitingClarificationBatchCards(page).count(), {
      timeout: 30_000,
    })
    .toBe(1);

  const dimensions: string[] = [];
  const batches: JsonRecord[] = [];
  for (
    let batchIndex = 0;
    batchIndex < MAX_CLARIFICATION_BATCHES;
    batchIndex += 1
  ) {
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

    const selections: JsonRecord[] = [];
    for (const question of questions) {
      const dimensionId = String(question.dimensionId || "");
      const questionText = String(question.question || "");
      expect(dimensionId).not.toBe("");
      expect(questionText).not.toBe("");
      expect(dimensions).not.toContain(dimensionId);
      dimensions.push(dimensionId);

      const group = card.getByRole("group", {
        name: questionText,
        exact: true,
      });
      await expect(group).toBeVisible();
      if (
        dimensionId === "route_decision.detour_tolerance" &&
        question.allowFreeText === true
      ) {
        const manualInput = group.getByRole("textbox", {
          name: `${questionText}的补充内容`,
          exact: true,
        });
        await manualInput.fill(MANUAL_DETOUR_PREFERENCE);
        await expect(
          group.getByRole("radio", { name: "其他，我来补充" }),
        ).toBeChecked();
        selections.push({
          dimensionId,
          mode: "manual_inline",
          manualValue: MANUAL_DETOUR_PREFERENCE,
        });
        continue;
      }

      const option = chooseClarificationOption(dimensionId, question);
      const label = String(option.label || "");
      expect(label).not.toBe("");
      await group.getByRole("radio", { name: label, exact: true }).check();
      selections.push({
        dimensionId,
        mode: "persisted_option",
        optionId: String(option.id || ""),
        label,
        semanticValue: option.semanticValue,
      });
    }

    const submit = card.getByRole("button", {
      name: "确认并开始规划",
      exact: true,
    });
    await expect(submit).toBeEnabled();
    const sourceAssistantTurnId = String(
      (await card.getAttribute("data-source-turn-id")) || "",
    );
    const streamsBefore = responses.length;
    await submit.click();
    await waitForSettledStreams(page, responses, streamsBefore + 1);
    expect(responses.length).toBe(streamsBefore + 1);
    batches.push({
      batchIndex,
      checkpointId: String(checkpoint.checkpointId || ""),
      checkpointFingerprint: String(checkpoint.fingerprint || ""),
      sourceAssistantTurnId,
      questionCount: questions.length,
      selections,
      streamRequestDelta: 1,
    });
  }

  expect(await awaitingClarificationBatchCards(page).count()).toBe(0);
  return { batches, dimensions };
}

function chooseClarificationOption(
  dimensionId: string,
  question: JsonRecord,
): JsonRecord {
  const options = asArray(question.options).map((value) => asRecord(value));
  if (options.length < 2 || options.length > 3) {
    throw new Error(
      `clarification option count out of contract: ${dimensionId}`,
    );
  }
  const preferred = options.find((option) => {
    const semantic = JSON.stringify(asRecord(option.semanticValue));
    if (dimensionId === "route_decision.mobility_profile") {
      return /"transportMode":"(?:transit|public_transit)"/.test(semantic);
    }
    if (dimensionId === "route_decision.detour_tolerance") {
      return /"maxDetourRatio":0\.(?:1[0-9]?|2)/.test(semantic);
    }
    return false;
  });
  return preferred || options[0];
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
  throw new Error("current v2 clarification checkpoint missing");
}

function currentComparisonState(session: JsonRecord): ComparisonState {
  const turns = asArray(session.turns).map((value) => asRecord(value));
  const latestScopedTurn = [...turns]
    .reverse()
    .find(
      (turn) =>
        String(turn.planningSelectionRootTurnId || "") &&
        String(turn.rootPortfolioId || ""),
    );
  if (!latestScopedTurn) {
    throw new Error("comparison root identity missing from current session");
  }
  const planningRootId = String(
    latestScopedTurn.planningSelectionRootTurnId || "",
  );
  const portfolioId = String(latestScopedTurn.rootPortfolioId || "");
  const scopedTurns = turns.filter(
    (turn) =>
      String(turn.planningSelectionRootTurnId || "") === planningRootId &&
      String(turn.rootPortfolioId || "") === portfolioId,
  );
  const byProposalId = new Map<string, JsonRecord>();
  for (const turn of scopedTurns) {
    if (turn.comparisonProjectionUpdateMode === "replace") {
      byProposalId.clear();
    }
    for (const projection of asArray(turn.comparisonProjections).map((value) =>
      asRecord(value),
    )) {
      const proposalId = String(projection.proposalId || "");
      if (proposalId) byProposalId.set(proposalId, projection);
    }
  }
  const summaryTurn = [...scopedTurns]
    .reverse()
    .find((turn) => Object.keys(asRecord(turn.comparisonSummary)).length > 0);
  const comparisonSummary = asRecord(summaryTurn?.comparisonSummary);
  if (!Object.keys(comparisonSummary).length) {
    throw new Error("authoritative comparison summary missing");
  }
  // Capabilities are turn-scoped. Never resurrect an earlier continuation
  // merely because the immutable historical option still says "offered";
  // an exhausted latest turn intentionally omits it.
  const choiceTurn = summaryTurn || latestScopedTurn;
  const continueChoice = choiceTurn
    ? asArray(choiceTurn.choiceOptions)
        .map((value) => asRecord(value))
        .find(
          (choice) =>
            choice.action === "continue_plan_expansion" &&
            !["consumed", "stale", "expired", "failed_terminal"].includes(
              String(choice.lifecycle || "offered"),
            ),
        ) || null
    : null;
  const projections = [...byProposalId.values()];
  const ready = projections.filter(isReadyProjection);
  const partial = projections.filter(
    (projection) => !isReadyProjection(projection),
  );
  return {
    comparisonSummary,
    continueChoice,
    planningRootId,
    portfolioId,
    projections,
    ready,
    partial,
    sourceAssistantTurnId: String(
      choiceTurn?.id || summaryTurn?.id || latestScopedTurn.id || "",
    ),
  };
}

function isReadyProjection(projection: JsonRecord): boolean {
  if (projection.adoptionReady !== true) return false;
  if (projection.confirmationPassed === false) return false;
  if (asArray(projection.blockingReasons).length > 0) return false;
  if (projection.confirmationPassed === true) return true;
  if (projection.workflowMode !== "simple_direction_v1") return true;
  const expected = Number(projection.routeExpectedLegCount || 0);
  const verified = Number(projection.routeVerifiedLegCount || 0);
  const routeStatus = String(projection.routeStatus || "");
  const routeComplete =
    expected > 0
      ? verified === expected && routeStatus === "route_ready"
      : ["route_ready", "route_not_required"].includes(routeStatus);
  return routeComplete && projection.nextAction === "confirm_edit";
}

async function assertComparisonUi(page: Page, state: ComparisonState) {
  const panel = page.locator('section[aria-label="Plan comparison"]');
  await expect(panel).toBeVisible();
  const readyCards = panel.locator(
    'article[data-section="ready"][data-adoption-ready="true"][data-confirmation-passed="true"]',
  );
  const partialCards = panel.locator('article[data-section="partial"]');
  const expectedReady = Number(state.comparisonSummary.adoptionReadyCount || 0);
  const expectedPartial = Number(
    state.comparisonSummary.repairablePartialCount || 0,
  );
  await expect(readyCards).toHaveCount(expectedReady);
  await expect(panel.getByText(`可确认 ${expectedReady} 个`)).toBeVisible();
  await expect(panel.getByText(`待补全 ${expectedPartial} 个`)).toBeVisible();
  if (expectedPartial > 0) {
    const partialSection = panel.locator("details.comparison-partial-section");
    await expect(partialSection).toBeVisible();
    await partialSection.locator("summary").click();
    await expect(partialCards).toHaveCount(expectedPartial);
    await expect(
      partialCards.getByRole("button", { name: /^确认编辑「.+」$/ }),
    ).toHaveCount(0);
  } else {
    await expect(partialCards).toHaveCount(0);
  }
  return {
    readyCardCount: await readyCards.count(),
    partialCardCount: await partialCards.count(),
    readyTitles: await readyCards.locator("h3").allTextContents(),
    summaryText: (
      await panel.locator(".comparison-summary-strip").innerText()
    ).trim(),
    frontierText:
      (await panel.locator(".comparison-frontier-status").count()) > 0
        ? (
            await panel.locator(".comparison-frontier-status").innerText()
          ).trim()
        : "",
  };
}

function debugBundleCounts(bundle: JsonRecord) {
  const sections = asRecord(bundle.sections);
  return {
    proposalCount: asArray(sections.PROPOSALS).length,
    choiceExecutionCount: asArray(sections.CHOICE_EXECUTIONS).length,
    itineraryVersionCount: asArray(sections.ITINERARY_VERSIONS).length,
    patchCount: asArray(sections.PATCHES).length,
    formalRouteWriteCount: asArray(sections.ROUTE_EVIDENCE).length,
  };
}

function currentFrontierEvidence(
  bundle: JsonRecord,
  portfolioId: string,
): JsonRecord {
  const sections = asRecord(bundle.sections);
  const portfolio = asArray(sections.PORTFOLIOS)
    .map((value) => asRecord(value))
    .find((row) => String(row.id || "") === portfolioId);
  if (!portfolio)
    throw new Error(`portfolio missing from debug bundle: ${portfolioId}`);
  const summary = asRecord(portfolio.summary_json);
  const frontier = asRecord(summary.simpleDirectionFrontier);
  expect(frontier.schemaVersion).toBe("simple-direction-frontier-v1");
  expect(String(frontier.qualificationEvidenceFingerprint || "")).toMatch(
    /^[0-9a-f]{64}$/i,
  );
  return {
    schemaVersion: frontier.schemaVersion,
    planningRootId: frontier.planningRootId,
    requestContractFingerprint: frontier.requestContractFingerprint,
    qualificationEvidenceFingerprint: frontier.qualificationEvidenceFingerprint,
    frontierStatus: frontier.frontierStatus,
    remainingQualifiedEntityCount: frontier.remainingQualifiedEntityCount,
    qualifiedEntityFrontier: frontier.qualifiedEntityFrontier,
    slotFrontiers: frontier.slotFrontiers,
    attempts: summary.simpleDirectionFrontierAttempts,
    comparisonSummary: summary.comparisonSummary,
  };
}

function proposalEvidenceFromBundle(
  bundle: JsonRecord,
  portfolioId: string,
): JsonRecord[] {
  const sections = asRecord(bundle.sections);
  return asArray(sections.PROPOSALS)
    .map((value) => asRecord(value))
    .filter((row) => String(row.portfolio_id || "") === portfolioId)
    .map((row) => {
      const snapshot = asRecord(row.snapshot_json);
      const verifier = asRecord(row.verifier_json);
      const segments: JsonRecord[] = [];
      for (const [dayIndex, dayValue] of asArray(snapshot.days).entries()) {
        const day = asRecord(dayValue);
        for (const [segmentIndex, segmentValue] of asArray(
          day.segments,
        ).entries()) {
          const segment = asRecord(segmentValue);
          const semantic = asRecord(segment.semanticMetadata);
          const constraints = asRecord(semantic.scheduleConstraints);
          const poi = asRecord(segment.poi);
          const independence = firstNonEmptyRecord(
            poi.experienceIndependenceEvidence,
            semantic.experienceIndependenceEvidence,
            constraints.experienceIndependenceEvidence,
          );
          segments.push({
            dayNumber: Number(day.dayNumber || dayIndex + 1),
            date: String(day.date || ""),
            segmentIndex: segmentIndex + 1,
            startTime: String(segment.startTime || ""),
            endTime: String(segment.endTime || ""),
            intentType: String(
              semantic.intentType || poi.intentType || segment.kind || "",
            ),
            planningSlotId: String(semantic.planningSlotId || ""),
            required: semantic.required === true,
            requirementLevel: String(semantic.requirementLevel || ""),
            poi: {
              name: String(poi.name || ""),
              amapId: String(poi.amapId || ""),
              city: String(poi.city || ""),
              parentPoiId: String(poi.parentPoiId || poi.parent || "") || null,
              indoorParentPoiId: String(poi.indoorParentPoiId || "") || null,
              providerType: String(poi.providerType || poi.type || ""),
              providerTypeCode: String(poi.providerTypeCode || ""),
            },
            physicalGroupId:
              String(independence.physicalGroupId || poi.amapId || "") || null,
            independenceStatus: String(independence.status || "") || null,
            experienceIndependenceEvidence: independence,
          });
        }
      }
      const campusSegments = segments.filter((segment) =>
        /campus|university|higher_education/.test(
          String(segment.intentType || ""),
        ),
      );
      const parkSegments = segments.filter(
        (segment) => String(segment.intentType || "") === "park",
      );
      return {
        proposalId: String(row.id || ""),
        lifecycleStatus: String(row.status || ""),
        title: String(snapshot.title || snapshot.displayTitle || ""),
        confirmationPassed:
          verifier.confirmationPassed === true ||
          asRecord(snapshot.portfolioVerifier).confirmationPassed === true,
        campusPhysicalGroups: campusSegments.map((segment) =>
          String(segment.physicalGroupId || ""),
        ),
        campusAmapIds: campusSegments.map((segment) =>
          String(asRecord(segment.poi).amapId || ""),
        ),
        parkIndependenceStatuses: parkSegments.map((segment) =>
          String(segment.independenceStatus || ""),
        ),
        segments,
        routeEvidence: snapshot.simpleOpenRouteAssignment,
        noveltyEvidence: snapshot.simpleDirectionNoveltyEvidence,
        qualificationConstraint: asRecord(snapshot.requestIntentContract)
          .entityQualificationConstraint,
        titleEvidence: snapshot.portfolioTitleEvidence,
        titleGeneration: snapshot.portfolioTitleGeneration,
        verifier,
      };
    });
}

function assertReadyProposalEvidence(
  state: ComparisonState,
  proposalEvidence: JsonRecord[],
  frontierEvidence: JsonRecord,
) {
  const frontierEntities = asArray(
    frontierEvidence.qualifiedEntityFrontier,
  ).map((value) => asRecord(value));
  const campusPairs = new Set<string>();
  for (const projection of state.ready) {
    const proposalId = String(projection.proposalId || "");
    const evidence = proposalEvidence.find(
      (candidate) => String(candidate.proposalId || "") === proposalId,
    );
    if (!evidence)
      throw new Error(`ready proposal evidence missing: ${proposalId}`);
    expect(evidence.confirmationPassed).toBe(true);
    const segments = asArray(evidence.segments).map((value) => asRecord(value));
    expect(segments).toHaveLength(6);
    const expectedDays = [
      {
        dayNumber: 1,
        date: "2026-10-01",
        planningSlotIds: [
          "day1_morning_campus",
          "day1_lunch",
          "day1_evening_park",
        ],
      },
      {
        dayNumber: 2,
        date: "2026-10-02",
        planningSlotIds: [
          "day2_morning_campus",
          "day2_lunch",
          "day2_evening_park",
        ],
      },
    ];
    for (const expectedDay of expectedDays) {
      const daySegments = segments
        .filter(
          (segment) => Number(segment.dayNumber || 0) === expectedDay.dayNumber,
        )
        .sort(
          (left, right) =>
            Number(left.segmentIndex || 0) - Number(right.segmentIndex || 0),
        );
      expect(daySegments).toHaveLength(3);
      expect(daySegments.map((segment) => String(segment.date || ""))).toEqual([
        expectedDay.date,
        expectedDay.date,
        expectedDay.date,
      ]);
      expect(
        daySegments.map((segment) => String(segment.intentType || "")),
      ).toEqual(["campus_visit", "meal", "park"]);
      expect(
        daySegments.map((segment) => String(segment.planningSlotId || "")),
      ).toEqual(expectedDay.planningSlotIds);
      expect(
        daySegments.every(
          (segment) =>
            String(segment.startTime || "") >= "09:00" &&
            String(segment.endTime || "") <= "19:00",
        ),
      ).toBe(true);
      expect(daySegments[0].required).toBe(true);
      expect(daySegments[0].requirementLevel).toBe("hard");
      expect(daySegments[1].startTime).toBe("12:00");
      expect(String(daySegments[1].endTime || "") <= "13:30").toBe(true);
    }
    const campusAmapIds = asArray(evidence.campusAmapIds).map((value) =>
      String(value || "").toUpperCase(),
    );
    expect(campusAmapIds).toHaveLength(2);
    expect(new Set(campusAmapIds).size).toBe(2);
    for (const amapId of campusAmapIds) {
      expect(amapId).toMatch(/^B[0-9A-Z]{8,31}$/);
      const frontierEntity = frontierEntities.find(
        (entity) =>
          String(entity.canonicalAmapId || "").toUpperCase() === amapId &&
          ["used_ready", "grounded"].includes(String(entity.state || "")),
      );
      expect(frontierEntity).toBeTruthy();
      const qualification = asRecord(frontierEntity?.qualificationBinding);
      expect(qualification.qualificationScheme).toBe(
        "moe_project_classification",
      );
      expect(qualification.qualificationValue).toBe("985");
      expect(String(qualification.locality || "")).toBe("北京");
      expect(String(qualification.canonicalName || "")).not.toBe(
        "北京科技大学",
      );
    }
    const campusPair = asArray(evidence.campusPhysicalGroups)
      .map((value) => String(value || ""))
      .join("|");
    expect(campusPair).not.toBe("");
    expect(campusPairs.has(campusPair)).toBe(false);
    campusPairs.add(campusPair);

    const parkStatuses = asArray(evidence.parkIndependenceStatuses).map(
      (value) => String(value || ""),
    );
    expect(parkStatuses).toHaveLength(2);
    expect(
      parkStatuses.every((status) => status === "standalone_verified"),
    ).toBe(true);

    const mealSegments = segments.filter(
      (segment) => String(segment.intentType || "") === "meal",
    );
    const parkSegments = segments.filter(
      (segment) => String(segment.intentType || "") === "park",
    );
    expect(mealSegments).toHaveLength(2);
    expect(parkSegments).toHaveLength(2);
    for (const meal of mealSegments) {
      const poi = asRecord(meal.poi);
      expect(String(poi.city || "")).toMatch(/^北京/);
      expect(String(poi.providerType || "")).toContain("北京菜");
      expect(
        `${String(poi.name || "")} ${String(poi.providerType || "")}`,
      ).not.toMatch(/食堂|canteen/i);
    }
    expect(
      new Set(
        mealSegments.map((segment) =>
          String(asRecord(segment.poi).amapId || ""),
        ),
      ).size,
    ).toBe(2);
    expect(
      new Set(
        parkSegments.map((segment) =>
          String(asRecord(segment.poi).amapId || ""),
        ),
      ).size,
    ).toBe(2);

    const routeEvidence = asRecord(evidence.routeEvidence);
    const expectedPairs = asArray(routeEvidence.expectedPairs);
    const verifiedPairs = asArray(routeEvidence.verifiedPairs).map((value) =>
      asRecord(value),
    );
    expect(routeEvidence.schemaVersion).toBe("simple-open-route-evidence-v2");
    expect(routeEvidence.routeCoverageComplete).toBe(true);
    expect(routeEvidence.topologyCompliance).toBe("verified");
    expect(routeEvidence.adjacentLegCompliance).toBe("verified");
    expect(expectedPairs).toHaveLength(4);
    expect(verifiedPairs).toHaveLength(4);
    for (const expectedDay of expectedDays) {
      const dayExpectedPairs = expectedPairs
        .map((value) => asRecord(value))
        .filter((pair) => Number(pair.dayNumber || 0) === expectedDay.dayNumber)
        .sort(
          (left, right) =>
            Number(left.pairOrdinal || 0) - Number(right.pairOrdinal || 0),
        );
      const dayVerifiedPairs = verifiedPairs
        .filter((pair) => Number(pair.dayNumber || 0) === expectedDay.dayNumber)
        .sort(
          (left, right) =>
            Number(left.pairOrdinal || 0) - Number(right.pairOrdinal || 0),
        );
      const daySegments = segments
        .filter(
          (segment) => Number(segment.dayNumber || 0) === expectedDay.dayNumber,
        )
        .sort(
          (left, right) =>
            Number(left.segmentIndex || 0) - Number(right.segmentIndex || 0),
        );
      const dayAmapIds = daySegments.map((segment) =>
        String(asRecord(segment.poi).amapId || ""),
      );
      const daySegmentIds = daySegments.map((segment) =>
        String(segment.planningSlotId || ""),
      );
      const expectedSegmentPairs = [
        [daySegmentIds[0], daySegmentIds[1]],
        [daySegmentIds[1], daySegmentIds[2]],
      ];
      expect(dayExpectedPairs).toHaveLength(2);
      expect(dayVerifiedPairs).toHaveLength(2);
      expect(
        dayExpectedPairs.map((pair) => [
          String(pair.fromAmapId || ""),
          String(pair.toAmapId || ""),
        ]),
      ).toEqual([
        [dayAmapIds[0], dayAmapIds[1]],
        [dayAmapIds[1], dayAmapIds[2]],
      ]);
      expect(
        dayExpectedPairs.map((pair) => [
          String(pair.fromSegmentId || ""),
          String(pair.toSegmentId || ""),
        ]),
      ).toEqual(expectedSegmentPairs);
      expect(
        dayVerifiedPairs.map((pair) => [
          String(pair.fromAmapId || ""),
          String(pair.toAmapId || ""),
        ]),
      ).toEqual([
        [dayAmapIds[0], dayAmapIds[1]],
        [dayAmapIds[1], dayAmapIds[2]],
      ]);
      expect(
        dayVerifiedPairs.map((pair) => [
          String(pair.fromSegmentId || ""),
          String(pair.toSegmentId || ""),
        ]),
      ).toEqual(expectedSegmentPairs);
    }
    expect(
      verifiedPairs.every(
        (pair) =>
          String(pair.provider || "") === "amap-webservice" &&
          String(pair.queriedAt || "").length > 0 &&
          /^[a-f0-9]{64}$/i.test(
            String(pair.providerEvidenceFingerprint || ""),
          ) &&
          Number(pair.durationSeconds || 0) > 0 &&
          Number(pair.durationSeconds || 0) <= 45 * 60 &&
          Number(pair.distanceMeters || 0) > 0 &&
          ["transit", "public_transit"].includes(
            String(pair.transportMode || ""),
          ),
      ),
    ).toBe(true);

    const novelty = asRecord(evidence.noveltyEvidence);
    expect(novelty.schemaVersion).toBe("simple-direction-novelty-v3");
    expect(novelty.passed).toBe(true);
    const titleGeneration = asRecord(evidence.titleGeneration);
    expect(Number(titleGeneration.attemptCount || 0)).toBeLessThanOrEqual(2);
    expect(String(titleGeneration.titleDecisionSource || "")).not.toBe("");
  }
}

function assertSiblingTitleShapeDifference(titles: string[]) {
  const normalized = titles.map(normalizeChineseTitle);
  expect(new Set(normalized).size).toBe(normalized.length);
  for (let leftIndex = 0; leftIndex < normalized.length; leftIndex += 1) {
    for (
      let rightIndex = leftIndex + 1;
      rightIndex < normalized.length;
      rightIndex += 1
    ) {
      const left = Array.from(normalized[leftIndex]);
      const right = Array.from(normalized[rightIndex]);
      expect(left.slice(0, 2).join("")).not.toBe(right.slice(0, 2).join(""));
      expect(left.slice(-2).join("")).not.toBe(right.slice(-2).join(""));
      expect(
        bigramJaccard(normalized[leftIndex], normalized[rightIndex]),
      ).toBeLessThan(0.5);
    }
  }
}

function normalizeChineseTitle(value: string): string {
  return Array.from(value.normalize("NFKC"))
    .filter((character) => /[\u3400-\u9fff]/.test(character))
    .join("");
}

function bigramJaccard(left: string, right: string): number {
  const grams = (value: string) => {
    const characters = Array.from(value);
    const output = new Set<string>();
    for (let index = 0; index < characters.length - 1; index += 1) {
      output.add(`${characters[index]}${characters[index + 1]}`);
    }
    return output;
  };
  const leftGrams = grams(left);
  const rightGrams = grams(right);
  const union = new Set([...leftGrams, ...rightGrams]);
  if (!union.size) return 0;
  let intersection = 0;
  for (const gram of leftGrams) {
    if (rightGrams.has(gram)) intersection += 1;
  }
  return intersection / union.size;
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

function selectedChoiceFromPayload(payload: JsonRecord) {
  const selected = asRecord(asRecord(payload.context).selectedAgentChoice);
  return {
    sourceAssistantTurnId: String(selected.sourceAssistantTurnId || ""),
    choiceId: String(selected.choiceId || ""),
  };
}

function findChoiceRequest(
  requests: CapturedStreamRequest[],
  sourceAssistantTurnId: string,
  choiceId: string,
) {
  return requests.find((captured) => {
    const selected = selectedChoiceFromPayload(captured.body);
    return (
      selected.sourceAssistantTurnId === sourceAssistantTurnId &&
      selected.choiceId === choiceId
    );
  });
}

async function adoptionAttemptEvidence({
  runId,
  sessionId,
  proposalId,
  expectedChoice,
  preflight,
  capturedRequest,
  requestsSinceClick,
  responsesSinceClick,
}: {
  runId: string;
  sessionId: string;
  proposalId: string;
  expectedChoice: JsonRecord;
  preflight: JsonRecord;
  capturedRequest?: CapturedStreamRequest;
  requestsSinceClick: CapturedStreamRequest[];
  responsesSinceClick: Response[];
}) {
  const expectedSourceTurnId = String(
    expectedChoice.sourceAssistantTurnId || "",
  );
  const expectedChoiceId = String(expectedChoice.choiceId || "");
  const matchedResponse = responsesSinceClick.find((response) => {
    const captured = captureStreamRequest(response.request());
    if (!captured) return false;
    const selected = selectedChoiceFromPayload(captured.body);
    return (
      selected.sourceAssistantTurnId === expectedSourceTurnId &&
      selected.choiceId === expectedChoiceId
    );
  });
  let responseEvidence: JsonRecord = {
    observed: false,
    responseCountSinceClick: responsesSinceClick.length,
  };
  if (matchedResponse) {
    try {
      const body = await Promise.race([
        matchedResponse.text(),
        new Promise<never>((_resolve, reject) =>
          setTimeout(() => reject(new Error("response_body_timeout")), 5_000),
        ),
      ]);
      responseEvidence = {
        observed: true,
        responseCountSinceClick: responsesSinceClick.length,
        httpStatus: matchedResponse.status(),
        contentType: String(matchedResponse.headers()["content-type"] || ""),
        bodySha256: sha256(body),
        bodyUtf8Bytes: Buffer.byteLength(body, "utf8"),
        ...summarizeNdjson(body),
      };
    } catch (error) {
      responseEvidence = {
        observed: true,
        responseCountSinceClick: responsesSinceClick.length,
        httpStatus: matchedResponse.status(),
        contentType: String(matchedResponse.headers()["content-type"] || ""),
        bodyReadError: error instanceof Error ? error.name : "Error",
      };
    }
  }
  const selectedAgentChoice = capturedRequest
    ? selectedChoiceFromPayload(capturedRequest.body)
    : null;
  return {
    schemaVersion: "simple-direction-adoption-attempt-v1",
    runId,
    sessionId,
    proposalId,
    expectedChoice,
    preflight,
    requestCountSinceClick: requestsSinceClick.length,
    request: capturedRequest
      ? {
          method: "POST",
          urlPath: new URL(capturedRequest.url).pathname,
          bodySha256: sha256(capturedRequest.rawBody),
          bodyUtf8Bytes: Buffer.byteLength(capturedRequest.rawBody, "utf8"),
          selectedAgentChoice,
        }
      : null,
    response: responseEvidence,
  };
}

async function waitForSessionIdle(page: Page, sessionId: string) {
  let latest: JsonRecord = {};
  await expect
    .poll(
      async () => {
        latest = await fetchJson(
          page,
          `${API_BASE}/agent/sessions/${sessionId}/reasoning-statuses`,
        );
        return latest.active === true;
      },
      { timeout: 30_000 },
    )
    .toBe(false);
  return {
    active: latest.active === true,
    activeTurnId: String(latest.activeTurnId || "") || null,
  };
}

function choiceIdentity(choice: JsonRecord, sourceAssistantTurnId: string) {
  return {
    sourceAssistantTurnId: String(
      choice.sourceAssistantTurnId || sourceAssistantTurnId,
    ),
    choiceId: String(choice.id || choice.choiceId || ""),
    action: String(choice.action || ""),
    scopeKind: String(choice.scopeKind || ""),
    planningSelectionRootTurnId: String(
      choice.planningSelectionRootTurnId || "",
    ),
    rootPortfolioId: String(choice.rootPortfolioId || ""),
    requestContractFingerprint: String(choice.requestContractFingerprint || ""),
  };
}

function summarizeNdjson(body: string) {
  const events = body
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      try {
        return asRecord(JSON.parse(line));
      } catch {
        return {};
      }
    });
  const messageResponse = [...events]
    .reverse()
    .find((event) => event.event === "message_response");
  const responseData = asRecord(messageResponse?.data);
  const assistantTurn = asRecord(responseData.assistantTurn);
  const errorEvents = events
    .filter((event) => event.event === "error")
    .map((event) => asRecord(event.data));
  return {
    eventCount: events.length,
    eventNames: events
      .map((event) => String(event.event || ""))
      .filter(Boolean)
      .slice(0, 64),
    assistantTurnId: String(assistantTurn.id || ""),
    executionStatus: String(
      asRecord(assistantTurn.structuredChoiceTrace).executionStatus || "",
    ),
    resultVersionId:
      String(
        asRecord(assistantTurn.structuredChoiceTrace).resultVersionId || "",
      ) || null,
    errors: errorEvents.slice(0, 8).map((data) => ({
      code: String(data.code || ""),
      statusCode: Number(data.statusCode || 0) || null,
    })),
  };
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
              10 * 60 * 1000,
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

async function fetchJson(page: Page, url: string): Promise<JsonRecord> {
  return page.evaluate(async (target) => {
    const response = await fetch(target);
    if (!response.ok) throw new Error(`${target} returned ${response.status}`);
    return response.json();
  }, url);
}

async function writeJson(filePath: string, value: unknown) {
  await writeFile(filePath, JSON.stringify(value, null, 2), "utf8");
}

function firstNonEmptyRecord(...values: unknown[]): JsonRecord {
  for (const value of values) {
    const record = asRecord(value);
    if (Object.keys(record).length) return record;
  }
  return {};
}

function sha256(value: string): string {
  return createHash("sha256").update(value, "utf8").digest("hex");
}

function asRecord(value: unknown): JsonRecord {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as JsonRecord)
    : {};
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

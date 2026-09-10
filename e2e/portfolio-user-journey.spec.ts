import { expect, test, type Page, type Response } from "@playwright/test";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";

const USER_REQUEST =
  "今年国庆参观北京高校两日游，晚上看北京夜景。10月1日到2日，2天，中等预算，1人，公交地铁优先。每天午餐想体验当地特色美食。";
const API_BASE = process.env.TRIP_E2E_API_BASE_URL || "http://localhost:8000/api";
const STREAM_PATH = /\/api\/agent\/sessions\/[^/]+\/messages\/stream(?:\?|$)/;

type JsonRecord = Record<string, unknown>;

test("真实用户从需求到 Portfolio 对比、地图交互和单次采用", async ({ page, context }, testInfo) => {
  const artifactDir = path.resolve(process.env.TRIP_E2E_ARTIFACT_DIR || testInfo.outputDir);
  const runId = process.env.TRIP_E2E_RUN_ID || `playwright-${Date.now()}`;
  await mkdir(artifactDir, { recursive: true });

  const streamResponses: Response[] = [];
  page.on("response", (response) => {
    if (STREAM_PATH.test(response.url()) && response.request().method() === "POST") {
      streamResponses.push(response);
    }
  });

  await page.goto("/", { waitUntil: "domcontentloaded" });
  await expect(page.getByRole("form", { name: "Agent 对话输入" })).toBeVisible();

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
  await expect(page.getByLabel("选择 Agent 模型")).toBeVisible();

  // AMap's JS key and security code are returned only during app bootstrap.
  // Start the diagnostic trace after those checks so the retained trace is
  // useful for the user journey without persisting credentials.
  const tracePath = path.join(artifactDir, "trace.zip");
  await context.tracing.start({ screenshots: false, snapshots: false, sources: true });
  try {

  const textarea = page.getByRole("textbox", { name: "Agent 对话文本" });
  await textarea.fill(USER_REQUEST);
  await textarea.press("Enter");
  console.log("[journey] initial request submitted");
  await waitForSettledStreams(page, streamResponses, 1);
  console.log(`[journey] initial planning settled streams=${streamResponses.length}`);
  expect(streamResponses[0].status()).toBe(200);
  expect(streamResponses[0].headers()["content-type"] || "").toMatch(/ndjson/i);

  const clarificationSelections = await answerRequiredClarifications(page, streamResponses);
  console.log(`[journey] clarification selections=${clarificationSelections.join(",") || "none"}`);

  const initialContinuationAttempts = await continuePlanningUntilMaterialized(page, streamResponses, 8);
  console.log(`[journey] initial continuation attempts=${initialContinuationAttempts}`);

  const firstSession = await fetchJson(page, `${API_BASE}/agent/sessions/current`);
  const requirementEvidence = JSON.stringify(firstSession);
  expect(requirementEvidence).toContain(USER_REQUEST);
  expect(requirementEvidence).toMatch(/2026-10-0?1/);
  expect(requirementEvidence).toMatch(/2026-10-0?2/);
  expect(requirementEvidence).toMatch(/北京/);
  expect(requirementEvidence).toMatch(/高校|校园|北京大学|清华大学/);
  expect(requirementEvidence).toMatch(/夜景/);
  expect(requirementEvidence).toMatch(/午餐|特色美食/);
  await expect(page.locator("body")).toContainText("规划方向");
  await expect(page.locator("body")).toContainText(/已验证对比方案|验证正式对比方案/);

  await page.getByRole("tab", { name: "行程对比" }).click();
  const cards = page.locator("article[data-proposal-id]");
  let expansionAttempts = initialContinuationAttempts;
  for (let attempt = expansionAttempts; attempt < 4; attempt += 1) {
    await page.getByRole("tab", { name: "行程对比" }).click();
    if ((await cards.count()) >= 2) break;
    const continueButton = page
      .locator("button:not([disabled])")
      .filter({ hasText: /继续生成其他方案/ })
      .last();
    const canContinue = await continueButton.isVisible().catch(() => false);
    if (!canContinue) break;
    const before = streamResponses.length;
    console.log(`[journey] expansion attempt=${attempt + 1} streamsBefore=${before}`);
    await continueButton.click({ timeout: 10_000 });
    await waitForSettledStreams(page, streamResponses, before + 1);
    expansionAttempts += 1;
    await page.getByRole("tab", { name: "行程对比" }).click();
    console.log(`[journey] expansion settled attempt=${attempt + 1} cards=${await cards.count()}`);
    if ((await cards.count()) >= 2) break;
  }

  await page.getByRole("tab", { name: "行程对比" }).click();
  await expect
    .poll(() => cards.count(), { timeout: 10_000, message: "真实 Controller 必须产出至少两个可对比卡片" })
    .toBeGreaterThanOrEqual(2);
  const cardCount = await cards.count();
  expect(cardCount).toBeGreaterThanOrEqual(2);

  const formalCards = page.locator(
    'article[data-proposal-id][data-is-partial="false"][data-adoption-ready="true"]'
  );
  expect(await formalCards.count(), "至少需要一个 verifier 通过且可采用的正式 proposal").toBeGreaterThan(0);
  const selectedCard = formalCards.first();
  const selectedProposalId = await selectedCard.getAttribute("data-proposal-id");
  expect(selectedProposalId).toBeTruthy();
  await selectedCard.click();
  console.log(`[journey] focused proposal=${selectedProposalId}`);

  const mapStage = page.locator(".map-stage");
  await expect(mapStage).toHaveAttribute("data-map-mode", "plan_overview_preview", { timeout: 30_000 });
  await expect(mapStage).toHaveAttribute("data-map-can-navigate", "true");
  await expect(mapStage).toHaveAttribute("data-map-can-search", "false");
  await expect(mapStage).toHaveAttribute("data-map-can-mutate", "false");
  await expect(page.getByPlaceholder("搜索景点 / 餐厅 / 体验")).toHaveCount(0);
  await expect(page.getByRole("button", { name: /加入当前 Day|替换当前地点|忽略该 POI/ })).toHaveCount(0);

  await expect(mapStage.locator(".amap-base")).toBeVisible({ timeout: 45_000 });
  const amapSurface = mapStage.locator(".amap-maps");
  const box = await amapSurface.boundingBox();
  expect(box).not.toBeNull();
  if (box) {
    const start = { x: box.x + box.width * 0.62, y: box.y + box.height * 0.48 };
    await page.mouse.move(start.x, start.y);
    await page.mouse.down();
    await page.mouse.move(start.x - 180, start.y + 90, { steps: 18 });
    await page.mouse.up();
    await page.mouse.move(box.x + box.width * 0.18, box.y + box.height * 0.72);
    await page.mouse.wheel(0, -180);
  }
  await page.waitForTimeout(2500);
  console.log("[journey] map drag and wheel completed");

  const marker = page.locator("button.map-dom-marker.itinerary:not(.selected):visible").first();
  await expect(marker).toBeVisible({ timeout: 30_000 });
  await marker.focus({ timeout: 10_000 });
  await page.keyboard.press("Enter");
  await expect(page.locator(".poi-map-thumbnail")).toBeVisible();
  await expect(page.getByRole("button", { name: "查看来源", exact: true })).toBeVisible();
  await expect(page.locator(".map-density-comparison-note")).toContainText("只读方案预览");
  console.log("[journey] marker inspection completed");

  await page.getByRole("button", { name: "复制测试信息" }).click({ timeout: 10_000 });
  await expect(page.getByRole("button", { name: "复制测试信息" })).toContainText("已复制");
  const bundle = await page.evaluate(() => navigator.clipboard.readText());
  const bundlePath = path.join(artifactDir, "trip-test-bundle-v3.txt");
  await writeFile(bundlePath, bundle, "utf8");
  console.log(`[journey] V3 bundle copied bytes=${Buffer.byteLength(bundle, "utf8")}`);

  expect(bundle).toContain("TRIP_TEST_BUNDLE_VERSION=3");
  expect(Buffer.byteLength(bundle, "utf8")).toBeLessThanOrEqual(500 * 1024);
  const structuredChoices = bundleSection(bundle, "STRUCTURED CHOICE REQUESTS") as unknown[];
  const executions = bundleSection(bundle, "CHOICE EXECUTIONS") as JsonRecord[];
  const visibility = asRecord(bundleSection(bundle, "PORTFOLIO VISIBILITY"));
  const comparison = asRecord(bundleSection(bundle, "COMPARISON STATE"));
  const mapInteraction = asRecord(bundleSection(bundle, "MAP INTERACTION"));
  expect(structuredChoices.length).toBeGreaterThan(0);
  expect(executions.length).toBeGreaterThan(0);
  expect(visibility.visibleCardCount).toBe(cardCount);
  expect(visibility.verifiedComparisonProposalCount).toBeGreaterThan(0);
  assertCanonicalPoiEntities(visibility);
  expect(asArray(comparison.plans).length).toBe(cardCount);
  expect(mapInteraction.initialized).toBe(true);
  expect(mapInteraction.mode).toBe("plan_overview_preview");
  expect(asRecord(mapInteraction.capabilities).navigate).toBe(true);
  expect(asRecord(mapInteraction.capabilities).search).toBe(false);
  expect(asRecord(mapInteraction.capabilities).mutateItinerary).toBe(false);
  expect(Number(mapInteraction.dragCount || 0)).toBeGreaterThan(0);
  expect(Number(mapInteraction.wheelCount || 0)).toBeGreaterThan(0);
  expect(Number(mapInteraction.zoomCount || 0)).toBeGreaterThan(0);
  expect(Number(mapInteraction.moveCount || 0)).toBeGreaterThan(0);
  expect(mapInteraction.pointerTarget).toBeTruthy();
  expect(viewportChanged(mapInteraction)).toBe(true);

  const expansionExecutions = executions.filter((item) => item.action === "retry_model_planning");
  expect(expansionExecutions.length).toBeGreaterThan(0);
  for (const execution of expansionExecutions) {
    expect(String(execution.rootPortfolioId || "")).not.toBe("");
    expect(String(execution.planningSelectionRootTurnId || "")).not.toBe("");
    expect(String(execution.requestContractFingerprint || "")).not.toBe("");
    expect(execution.versionDelta).toBe(0);
    expect(execution.patchDelta).toBe(0);
    expect(execution.routeWriteDelta).toBe(0);
  }
  const successfulExpansion = expansionExecutions.find((item) => asRecord(item.outcome).succeeded === true);
  expect(successfulExpansion, "至少一次同根扩展必须新增 verifier 通过的正式 proposal").toBeTruthy();
  expect(Number(asRecord(successfulExpansion?.outcome).proposalDelta || 0)).toBeGreaterThan(0);
  expect(bundle).not.toMatch(/"source"\s*:\s*"(?:amap-fake|mock|synthetic)"/i);
  expect(bundle).not.toMatch(/"name"\s*:\s*"[^"]*(?:placeholder[_ -]?poi|占位地点|占位方案)[^"]*"/i);
  expect(bundle).not.toMatch(/Bearer\s+(?!\[REDACTED\])/i);
  expect(bundle).not.toMatch(
    /"(?:reasoning|reasoning_content|reasoningContent|reasoningText|chainOfThought)"\s*:\s*"(?!\[REDACTED\])/i
  );
  expect(bundle).not.toMatch(/[?&](?:key|token|secret)=((?!\[REDACTED\])[^&#\s]+)/i);

  const preAdoptionSession = await fetchJson(page, `${API_BASE}/agent/sessions/current`);
  const preAdoptionVersionId = String(preAdoptionSession.activeVersionId || "");
  const streamsBeforeAdoption = streamResponses.length;
  await page.getByRole("tab", { name: "行程对比" }).click();
  const adoptionCard = page.locator(
    `article[data-proposal-id="${selectedProposalId}"][data-is-partial="false"][data-adoption-ready="true"]`
  );
  await expect(adoptionCard).toBeVisible({ timeout: 10_000 });
  await adoptionCard.getByRole("button", { name: "采用此方案", exact: true }).click({ timeout: 10_000 });
  console.log(`[journey] adoption submitted proposal=${selectedProposalId}`);
  await waitForSettledStreams(page, streamResponses, streamsBeforeAdoption + 1);
  expect(streamResponses.length).toBe(streamsBeforeAdoption + 1);
  await expect(mapStage).toHaveAttribute("data-map-mode", "itinerary_edit", { timeout: 30_000 });
  await expect(mapStage).toHaveAttribute("data-map-can-search", "true");
  await expect(mapStage).toHaveAttribute("data-map-can-mutate", "true");
  await expect(page.getByPlaceholder("搜索景点 / 餐厅 / 体验")).toBeVisible();

  const postAdoptionSession = await fetchJson(page, `${API_BASE}/agent/sessions/current`);
  const postAdoptionVersionId = String(postAdoptionSession.activeVersionId || "");
  expect(postAdoptionVersionId).not.toBe("");
  expect(postAdoptionVersionId).not.toBe(preAdoptionVersionId);
  const result = {
    runId,
    sessionId: String(postAdoptionSession.sessionId || postAdoptionSession.id || ""),
    selectedProposalId,
    planningSelectionRootTurnId: visibility.planningSelectionRootTurnId,
    rootPortfolioId: visibility.rootPortfolioId,
    visibleProposalIds: asArray(visibility.plans).map((item) => asRecord(item).proposalId),
    formalProposalIds: asArray(visibility.plans)
      .map(asRecord)
      .filter((item) => item.isPartial !== true)
      .map((item) => item.proposalId),
    cardCount,
    verifiedComparisonProposalCount: visibility.verifiedComparisonProposalCount,
    expansionAttempts,
    clarificationSelections,
    streamRequestCount: streamResponses.length,
    preAdoptionVersionId,
    postAdoptionVersionId,
    bundlePath: path.basename(bundlePath),
    mapInteraction: {
      dragCount: mapInteraction.dragCount,
      wheelCount: mapInteraction.wheelCount,
      zoomCount: mapInteraction.zoomCount,
      moveCount: mapInteraction.moveCount,
      interactionStartCenter: mapInteraction.interactionStartCenter,
      interactionStartZoom: mapInteraction.interactionStartZoom,
      center: mapInteraction.center,
      zoom: mapInteraction.zoom
    }
  };
  await writeFile(path.join(artifactDir, "journey-result.json"), JSON.stringify(result, null, 2), "utf8");
  console.log(`[journey] completed session=${result.sessionId} version=${postAdoptionVersionId}`);
  await page.screenshot({ path: path.join(artifactDir, "adopted-itinerary.png"), fullPage: true });
  } finally {
    await context.tracing.stop({ path: tracePath });
  }
});

async function answerRequiredClarifications(page: Page, responses: Response[]) {
  const selections: string[] = [];
  let controllerRetries = 0;
  const maxControllerRetries = 2;
  const desiredSemantics = [
    /every_available_evening|every_allowed_day|each_evening|每个可用夜晚|每晚/,
    /public_city_view|public_outdoor|skyline_public_space|公共.*(?:户外|城市|观景)/
  ];
  for (let step = 0; step < desiredSemantics.length; step += 1) {
    const options = page.locator('button[data-semantic-value]:not([disabled])');
    let count = await options.count();
    while (count === 0 && controllerRetries < maxControllerRetries) {
      const retry = page
        .locator('button:not([disabled])')
        .filter({ hasText: /重试生成澄清问题|重试本轮/ })
        .last();
      if (!(await retry.isVisible().catch(() => false))) break;
      const before = responses.length;
      await retry.click({ timeout: 10_000 });
      await waitForSettledStreams(page, responses, before + 1);
      controllerRetries += 1;
      count = await options.count();
      console.log(`[journey] real controller retry=${controllerRetries} options=${count}`);
    }
    if (count === 0) {
      throw new Error(`real controller did not return clarification options after ${controllerRetries} retries`);
    }
    let selected = false;
    for (let index = 0; index < count; index += 1) {
      const option = options.nth(index);
      const semanticValue = String(await option.getAttribute("data-semantic-value") || "");
      const label = String(await option.textContent() || "").trim();
      const searchable = `${semanticValue} ${label}`;
      if (!desiredSemantics[step].test(searchable)) continue;
      const before = responses.length;
      await option.click({ timeout: 10_000 });
      await waitForSettledStreams(page, responses, before + 1);
      selections.push(semanticValue || label);
      selected = true;
      break;
    }
    if (!selected) {
      const visible = await options.evaluateAll((nodes) => nodes.map((node) => ({
        label: (node.textContent || "").trim(),
        semanticValue: node.getAttribute("data-semantic-value") || ""
      })));
      throw new Error(`unexpected clarification contract at step ${step + 1}: ${JSON.stringify(visible)}`);
    }
  }
  return selections;
}

async function continuePlanningUntilMaterialized(page: Page, responses: Response[], maxAttempts: number) {
  let attempts = 0;
  const attemptedScopes = new Set<string>();
  while (attempts < maxAttempts) {
    const formalProposal = page.locator(
      'article[data-proposal-id][data-is-partial="false"][data-adoption-ready="true"]'
    );
    const verifiedSummaryVisible = await page
      .getByText(/已验证对比方案|验证正式对比方案/)
      .last()
      .isVisible()
      .catch(() => false);
    if ((await formalProposal.count()) > 0 && verifiedSummaryVisible) return attempts;
    const actionPriority = [
      "resume_density_candidate",
      "expand_density_nearby",
      "refresh_density_candidates",
      "retry_model_planning",
      "portfolio_more_plans"
    ];
    // Only the newest assistant turn owns executable planning choices. Older
    // cards stay visible for audit/history and must never be used to resume a
    // superseded clarification checkpoint.
    const latestAssistantTurn = page.locator(".chat-row.assistant").last();
    let continueButton = page.locator('button[data-choice-action="missing"]');
    let selectedAction = "";
    let selectedScope = "";
    for (const action of actionPriority) {
      const candidates = latestAssistantTurn.locator(
        `button[data-choice-action="${action}"]:not([disabled])`
      );
      const count = await candidates.count();
      for (let index = count - 1; index >= 0; index -= 1) {
        const candidate = candidates.nth(index);
        if (!(await candidate.isVisible().catch(() => false))) continue;
        const scope = await candidate.evaluate((node) => {
          const button = node as HTMLButtonElement;
          return [
            button.dataset.choiceAction || "",
            button.dataset.briefId || "",
            button.dataset.poolId || "",
            button.dataset.planningSlotId || "",
            button.dataset.dayNumber || "",
            button.dataset.semanticValue || ""
          ].join("|");
        });
        // A fresh assistant turn may legitimately reissue the same bounded
        // model-planning retry after external route/provider evidence changes.
        // Density/portfolio scopes remain single-use, but blocking the new
        // retry button here made the browser stop one click before the server's
        // retry budget could advance the real journey.
        if (attemptedScopes.has(scope) && action !== "retry_model_planning") continue;
        continueButton = candidate;
        selectedAction = action;
        selectedScope = scope;
        break;
      }
      if (selectedAction) break;
    }
    if (!selectedAction) return attempts;
    const before = responses.length;
    attemptedScopes.add(selectedScope);
    await continueButton.click({ timeout: 10_000 });
    await waitForSettledStreams(page, responses, before + 1);
    attempts += 1;
    console.log(
      `[journey] planning continuation=${attempts} action=${selectedAction} scope=${selectedScope} streams=${responses.length}`
    );
  }
  return attempts;
}

async function waitForSettledStreams(page: Page, responses: Response[], minimumCount: number) {
  await expect.poll(() => responses.length, { timeout: 90_000 }).toBeGreaterThanOrEqual(minimumCount);
  let stablePasses = 0;
  let lastCount = -1;
  while (stablePasses < 3) {
    const current = [...responses];
    const errors = await Promise.all(current.map((response) => Promise.race([
      response.finished(),
      new Promise<Error>((resolve) => setTimeout(() => resolve(new Error("stream did not finish within 10 minutes")), 600_000))
    ])));
    expect(errors.filter(Boolean)).toEqual([]);
    for (const response of current) expect(response.status()).toBe(200);
    await page.waitForTimeout(800);
    const composerEnabled = await page.getByRole("textbox", { name: "Agent 对话文本" }).isEnabled();
    if (responses.length === current.length && responses.length === lastCount && composerEnabled) {
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

function bundleSection(bundle: string, name: string): unknown {
  const normalized = bundle.replace(/\r\n/g, "\n");
  const prefix = `=== ${name} ===\n`;
  const start = normalized.indexOf(prefix);
  if (start < 0) throw new Error(`Missing bundle section ${name}`);
  const bodyStart = start + prefix.length;
  const next = normalized.indexOf("\n=== ", bodyStart);
  const body = normalized.slice(bodyStart, next < 0 ? normalized.indexOf("\nEND_TRIP_TEST_BUNDLE", bodyStart) : next);
  return JSON.parse(body);
}

function asRecord(value: unknown): JsonRecord {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as JsonRecord) : {};
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function viewportChanged(mapInteraction: JsonRecord): boolean {
  const startCenter = asArray(mapInteraction.interactionStartCenter).map(Number);
  const center = asArray(mapInteraction.center).map(Number);
  const startZoom = Number(mapInteraction.interactionStartZoom);
  const zoom = Number(mapInteraction.zoom);
  const centerChanged =
    startCenter.length === 2 &&
    center.length === 2 &&
    (Math.abs(startCenter[0] - center[0]) > 1e-7 || Math.abs(startCenter[1] - center[1]) > 1e-7);
  const zoomChanged = Number.isFinite(startZoom) && Number.isFinite(zoom) && Math.abs(startZoom - zoom) > 1e-7;
  return centerChanged || zoomChanged;
}

function assertCanonicalPoiEntities(value: unknown) {
  const pois: JsonRecord[] = [];
  const visit = (current: unknown) => {
    if (Array.isArray(current)) {
      current.forEach(visit);
      return;
    }
    if (!current || typeof current !== "object") return;
    const record = current as JsonRecord;
    if (record.poi && typeof record.poi === "object" && !Array.isArray(record.poi)) {
      pois.push(record.poi as JsonRecord);
    }
    Object.values(record).forEach(visit);
  };
  visit(value);
  expect(pois.length, "真实行程或对比卡中至少应有一个 POI 实体").toBeGreaterThan(0);
  for (const poi of pois) {
    expect(poi.source).toBe("amap-place-search");
    expect(String(poi.amapId || "")).not.toBe("");
    expect(Number.isFinite(Number(poi.latitude))).toBe(true);
    expect(Number.isFinite(Number(poi.longitude))).toBe(true);
    expect(String(poi.name || "")).not.toMatch(/placeholder[_ -]?poi|占位地点|占位方案/i);
  }
}

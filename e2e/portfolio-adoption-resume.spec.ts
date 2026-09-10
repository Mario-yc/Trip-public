import { expect, test, type Page, type Response } from "@playwright/test";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";

const API_BASE = process.env.TRIP_E2E_API_BASE_URL || "http://localhost:8000/api";
const STREAM_PATH = /\/api\/agent\/sessions\/[^/]+\/messages\/stream(?:\?|$)/;

type JsonRecord = Record<string, unknown>;

test("从已验收的对比阶段恢复并完成正式方案单次采用", async ({ page, context }, testInfo) => {
  const artifactDir = path.resolve(process.env.TRIP_E2E_ARTIFACT_DIR || testInfo.outputDir);
  const runId = process.env.TRIP_E2E_RUN_ID || `playwright-${Date.now()}`;
  const seedBundlePath = process.env.TRIP_E2E_SEED_BUNDLE_PATH || "";
  expect(seedBundlePath, "resume run 必须携带前序 V3 证据包").not.toBe("");
  await mkdir(artifactDir, { recursive: true });
  const bundle = await readFile(seedBundlePath, "utf8");
  const visibility = asRecord(bundleSection(bundle, "PORTFOLIO VISIBILITY"));
  const mapInteraction = asRecord(bundleSection(bundle, "MAP INTERACTION"));
  const plans = asArray(visibility.plans).map(asRecord);
  const formalPlans = plans.filter((item) => item.isPartial !== true);
  expect(formalPlans.length).toBeGreaterThan(0);
  const selectedProposalId = String(formalPlans[0].proposalId || "");
  expect(selectedProposalId).not.toBe("");

  const streamResponses: Response[] = [];
  page.on("response", (response) => {
    if (STREAM_PATH.test(response.url()) && response.request().method() === "POST") {
      streamResponses.push(response);
    }
  });

  await page.goto("/", { waitUntil: "domcontentloaded" });
  await expect(page.getByRole("form", { name: "Agent 对话输入" })).toBeVisible();
  const providerStatus = await fetchJson(page, `${API_BASE}/providers/status`);
  expect(providerStatus.mode).toBe("default");
  expect(asRecord(providerStatus.agent).configured).toBe(true);
  expect(asRecord(asRecord(providerStatus.tools).amapWeather).configured).toBe(true);
  const mapConfig = await fetchJson(page, `${API_BASE}/map/config`);
  expect(mapConfig.enabled).toBe(true);
  expect(String(mapConfig.jsApiKey || "").length).toBeGreaterThan(8);

  const tracePath = path.join(artifactDir, "trace.zip");
  await context.tracing.start({ screenshots: false, snapshots: false, sources: true });
  try {
    const before = await fetchJson(page, `${API_BASE}/agent/sessions/current`);
    const preAdoptionVersionId = String(before.activeVersionId || "");
    expect(preAdoptionVersionId).not.toBe("");

    await page.getByRole("tab", { name: "行程对比" }).click();
    const adoptionCard = page.locator(
      `article[data-proposal-id="${selectedProposalId}"][data-is-partial="false"][data-adoption-ready="true"]`
    );
    await expect(adoptionCard).toBeVisible({ timeout: 30_000 });
    await adoptionCard.click();
    const mapStage = page.locator(".map-stage");
    await expect(mapStage).toHaveAttribute("data-map-mode", "plan_overview_preview", { timeout: 30_000 });
    await expect(mapStage).toHaveAttribute("data-map-can-navigate", "true");
    await expect(mapStage).toHaveAttribute("data-map-can-search", "false");
    await expect(mapStage).toHaveAttribute("data-map-can-mutate", "false");

    await page.getByRole("tab", { name: "行程对比" }).click();
    await expect(adoptionCard).toBeVisible({ timeout: 10_000 });
    await adoptionCard.getByRole("button", { name: "采用此方案", exact: true }).click();
    await waitForSettledStreams(page, streamResponses, 1);
    expect(streamResponses).toHaveLength(1);
    await assertSuccessfulMessageStream(streamResponses[0]);

    await expect(mapStage).toHaveAttribute("data-map-mode", "itinerary_edit", { timeout: 60_000 });
    await expect(mapStage).toHaveAttribute("data-map-can-search", "true");
    await expect(mapStage).toHaveAttribute("data-map-can-mutate", "true");
    await expect(page.getByPlaceholder("搜索景点 / 餐厅 / 体验")).toBeVisible();

    const after = await fetchJson(page, `${API_BASE}/agent/sessions/current`);
    const postAdoptionVersionId = String(after.activeVersionId || "");
    expect(postAdoptionVersionId).not.toBe("");
    expect(postAdoptionVersionId).not.toBe(preAdoptionVersionId);
    await writeFile(path.join(artifactDir, "trip-test-bundle-v3.txt"), bundle, "utf8");
    const result = {
      runId,
      sessionId: String(after.sessionId || after.id || ""),
      selectedProposalId,
      planningSelectionRootTurnId: visibility.planningSelectionRootTurnId,
      rootPortfolioId: visibility.rootPortfolioId,
      visibleProposalIds: plans.map((item) => item.proposalId),
      formalProposalIds: formalPlans.map((item) => item.proposalId),
      cardCount: Number(visibility.visibleCardCount || plans.length),
      verifiedComparisonProposalCount: visibility.verifiedComparisonProposalCount,
      expansionAttempts: asArray(bundleSection(bundle, "CHOICE EXECUTIONS")).filter(
        (item) => asRecord(item).action === "retry_model_planning"
      ).length,
      streamRequestCount: 1,
      preAdoptionVersionId,
      postAdoptionVersionId,
      bundlePath: "trip-test-bundle-v3.txt",
      resumedFromBundle: path.basename(seedBundlePath),
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
    await page.screenshot({ path: path.join(artifactDir, "adopted-itinerary.png"), fullPage: true });
  } finally {
    await context.tracing.stop({ path: tracePath });
  }
});

async function waitForSettledStreams(page: Page, responses: Response[], minimumCount: number) {
  await expect.poll(() => responses.length, { timeout: 90_000 }).toBeGreaterThanOrEqual(minimumCount);
  const current = [...responses];
  const errors = await Promise.all(current.map((response) => response.finished()));
  expect(errors.filter(Boolean)).toEqual([]);
  await expect(page.getByRole("textbox", { name: "Agent 对话文本" })).toBeEnabled({ timeout: 30_000 });
}

async function assertSuccessfulMessageStream(response: Response) {
  expect(response.status()).toBe(200);
  expect(response.headers()["content-type"] || "").toMatch(/ndjson/i);
  const events = (await response.text())
    .replace(/\r\n/g, "\n")
    .split("\n")
    .filter(Boolean)
    .map((line) => JSON.parse(line) as JsonRecord);
  const errorEvent = events.find((event) => event.event === "error");
  expect(errorEvent, `stream error: ${JSON.stringify(errorEvent)}`).toBeUndefined();
  expect(events.some((event) => event.event === "message_response")).toBe(true);
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
  const end = next < 0 ? normalized.indexOf("\nEND_TRIP_TEST_BUNDLE", bodyStart) : next;
  return JSON.parse(normalized.slice(bodyStart, end));
}

function asRecord(value: unknown): JsonRecord {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as JsonRecord) : {};
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

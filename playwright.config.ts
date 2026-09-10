import { defineConfig, devices } from "@playwright/test";
import path from "node:path";

const artifactRoot = process.env.TRIP_E2E_ARTIFACT_DIR
  ? path.resolve(process.env.TRIP_E2E_ARTIFACT_DIR)
  : path.resolve(".ai-runs", "live-e2e", "playwright-artifacts");

export default defineConfig({
  testDir: "./e2e",
  timeout: 25 * 60 * 1000,
  expect: { timeout: 30_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  outputDir: path.join(artifactRoot, "test-output"),
  reporter: [["line"], ["json", { outputFile: path.join(artifactRoot, "playwright-report.json") }]],
  use: {
    ...devices["Desktop Chrome"],
    baseURL: process.env.TRIP_E2E_FRONTEND_URL || "http://localhost:5173",
    geolocation: { longitude: 116.4074, latitude: 39.9042 },
    permissions: ["geolocation", "clipboard-read", "clipboard-write"],
    locale: "zh-CN",
    screenshot: "only-on-failure",
    // The test starts tracing only after the real provider/map configuration
    // checks, so credential-bearing map config responses never enter trace.zip.
    trace: "off",
    video: "on",
    viewport: { width: 1600, height: 1000 }
  }
});

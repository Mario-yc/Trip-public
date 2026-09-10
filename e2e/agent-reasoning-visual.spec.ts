import { expect, test, type Page } from "@playwright/test";

type Scenario = "completed" | "safe_noop" | "cancelled";

test.describe("Agent reasoning disclosure visual contract", () => {
  for (const scenario of ["completed", "safe_noop"] as const) {
    test(`${scenario} keeps reasoning and answer in one assistant group`, async ({ page }, testInfo) => {
      await installDeterministicAgentFixture(page, scenario);
      await page.goto("/", { waitUntil: "domcontentloaded" });

      const composer = page.getByRole("textbox", { name: "Agent 对话文本" });
      await composer.fill(scenario === "completed" ? "调整第 2 天下半天" : "帮我看看当前行程");
      await composer.press("Enter");

      const disclosure = page.getByLabel("Agent 思考摘要");
      await expect(disclosure).toBeVisible();
      await expect(disclosure.getByRole("button")).toHaveAttribute("aria-expanded", "true");
      await expect(page.locator(".chat-row.assistant .bot-avatar")).toHaveCount(1);
      await captureChatPane(page, testInfo.outputPath(`${scenario}-running.png`));

      await expect(page.getByText(finalAnswer(scenario))).toBeVisible();
      const group = page.locator('[data-assistant-response-group="turn_visual_assistant"]');
      await expect(group).toBeVisible();
      await expect(group.locator(".bot-avatar")).toHaveCount(1);
      await expect(group.getByLabel("Agent 思考摘要")).toHaveCount(1);
      const trigger = group.getByLabel("Agent 思考摘要").getByRole("button", { name: /思考了 \d+ 秒/ });
      await expect(trigger).toHaveAttribute("aria-expanded", "false");
      await expect(group).not.toContainText(/\d+ 个阶段|规划过程失败|处理未完成/);
      await captureChatPane(page, testInfo.outputPath(`${scenario}-completed-collapsed.png`));

      await trigger.click();
      await expect(trigger).toHaveAttribute("aria-expanded", "true");
      await expect(group.getByText("已读取当前行程和用户要求")).toHaveCount(1);
      await captureChatPane(page, testInfo.outputPath(`${scenario}-completed-expanded.png`));
      if (scenario === "completed") {
        await page.setViewportSize({ width: 430, height: 900 });
        await expect(group).toBeVisible();
        await page.screenshot({ path: testInfo.outputPath("completed-mobile-expanded.png"), fullPage: true });
      }
    });
  }

  test("cancelled run closes one disclosure without a failure card", async ({ page }, testInfo) => {
    await installDeterministicAgentFixture(page, "cancelled");
    await page.goto("/", { waitUntil: "domcontentloaded" });
    const composer = page.getByRole("textbox", { name: "Agent 对话文本" });
    await composer.fill("停止这次处理");
    await composer.press("Enter");

    await expect(page.getByLabel("Agent 思考摘要")).toBeVisible();
    await captureChatPane(page, testInfo.outputPath("cancelled-running.png"));
    await page.getByRole("button", { name: "停止本轮" }).click();

    const group = page.locator('[data-assistant-response-group="interrupted-agent-run"]');
    await expect(group).toBeVisible();
    await expect(group.locator(".bot-avatar")).toHaveCount(1);
    await expect(group.getByLabel("Agent 思考摘要")).toHaveCount(1);
    await expect(group.getByRole("button", { name: "已停止" })).toBeVisible();
    await expect(group.locator(".reasoning-status-spinner.active")).toHaveCount(0);
    await expect(page.locator("body")).not.toContainText("规划过程失败");
    await expect(page.locator("body")).not.toContainText(/operation was aborted/i);
    await captureChatPane(page, testInfo.outputPath("cancelled-completed.png"));
  });
});

async function installDeterministicAgentFixture(page: Page, scenario: Scenario) {
  await page.addInitScript((selectedScenario: Scenario) => {
    const nativeFetch = window.fetch.bind(window);
    const jsonResponse = (data: unknown, status = 200) =>
      new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } });
    const createdAt = "2026-08-21T00:00:00Z";
    const session = {
      sessionId: "sess_reasoning_visual",
      status: "active",
      city: "北京",
      title: "北京 AI 行程",
      activePlanId: null,
      activeVersionId: null,
      turns: [],
      itinerary: null,
      pendingPoiCandidates: []
    };
    const status = (
      sequence: number,
      semanticKey: string,
      state: "running" | "completed",
      summary: string,
      detail: string | null,
      phase: "context" | "verification" | "finalizing"
    ) => ({
      messageType: "reasoning_status",
      id: `reasoning_${sequence}`,
      sequence,
      runId: "run_reasoning_visual",
      semanticKey,
      phase,
      status: state,
      summary,
      detail,
      sourceEventType: phase === "finalizing" ? "agent_run_terminal" : "read_itinerary",
      sessionId: session.sessionId,
      turnId: "turn_visual_user",
      rootUserTurnId: "turn_visual_user",
      assistantTurnId: state === "completed" ? "turn_visual_assistant" : null,
      startedAt: createdAt,
      completedAt: state === "completed" ? "2026-08-21T00:00:02Z" : null,
      elapsedMs: sequence * 700,
      firstSequence: sequence,
      latestSequence: sequence,
      timestamp: createdAt
    });
    const completedStatuses = [
      status(2, "context_understanding", "completed", "已读取当前行程和用户要求", null, "context"),
      status(
        3,
        "schedule_validation",
        "completed",
        selectedScenario === "safe_noop" ? "已确认本轮只需查看信息，不修改行程" : "已核对第 2 天下半天的时间和地点",
        null,
        "verification"
      ),
      status(4, "result_synthesis", "completed", "处理完成，正在展示结果", null, "finalizing")
    ];
    const userTurn = {
      id: "turn_visual_user",
      role: "user",
      content: selectedScenario === "safe_noop" ? "帮我看看当前行程" : "调整第 2 天下半天",
      turnIndex: 1,
      status: "active",
      createdAt,
      updatedAt: createdAt
    };
    const assistantTurn = {
      id: "turn_visual_assistant",
      role: "assistant",
      content: selectedScenario === "safe_noop" ? "已查看当前行程；本轮无需修改，原有安排保持不变。" : "已将第 2 天下半天调整为更顺路的安排。",
      turnIndex: 2,
      status: "active",
      reasoningStatuses: completedStatuses,
      createdAt,
      updatedAt: createdAt
    };
    const response = {
      userTurn,
      assistantTurn,
      itinerary: null,
      version: null,
      pendingPoiCandidates: [],
      preferenceMemory: null,
      warnings: [],
      planningRun: null,
      planningSteps: [],
      toolEvents: [],
      reasoningStatuses: completedStatuses,
      executionMode: "single_root",
      terminalStatus: selectedScenario === "safe_noop" ? "needs_confirmation" : "success",
      agentDecisionCount: 1
    };

    window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = String(init?.method || "GET").toUpperCase();
      if (!url.includes("/api/")) return nativeFetch(input, init);
      if (url.endsWith("/providers/status")) return jsonResponse({ mode: "mock", default: [], mock: [] });
      if (url.endsWith("/map/config")) return jsonResponse({ enabled: false });
      if (url.endsWith("/preferences/extract")) return jsonResponse({ summaryCard: {} });
      if (url.endsWith("/agent/sessions/current")) return jsonResponse({ detail: "No active session" }, 404);
      if (url.endsWith("/agent/sessions") && method === "POST") return jsonResponse(session);
      if (url.endsWith("/agent/sessions") && method === "GET") return jsonResponse({ sessions: [] });
      if (/\/agent\/sessions\/[^/]+\/messages\/stream$/.test(url)) {
        const encoder = new TextEncoder();
        let closed = false;
        const timers: number[] = [];
        const stream = new ReadableStream<Uint8Array>({
          start(controller) {
            const send = (event: string, data: unknown) => {
              if (!closed) controller.enqueue(encoder.encode(`${JSON.stringify({ event, data })}\n`));
            };
            send("user_turn", userTurn);
            send(
              "reasoning_status",
              status(1, "context_understanding", "running", "正在核对你的要求和当前行程", "正在读取当前会话中的地点和时间安排。", "context")
            );
            if (selectedScenario !== "cancelled") {
              timers.push(
                window.setTimeout(() => send("reasoning_status", completedStatuses[0]), 450),
                window.setTimeout(
                  () =>
                    send(
                      "reasoning_status",
                      status(3, "schedule_validation", "running", "正在核对时间和地点约束", "只保留对用户有帮助的高层摘要。", "verification")
                    ),
                  800
                ),
                window.setTimeout(() => {
                  send("reasoning_status", completedStatuses[1]);
                  send("reasoning_status", completedStatuses[2]);
                  send("message_response", response);
                  closed = true;
                  controller.close();
                }, 1800)
              );
            }
            init?.signal?.addEventListener("abort", () => {
              timers.forEach((timer) => window.clearTimeout(timer));
              if (!closed) {
                closed = true;
                controller.error(new DOMException("The operation was aborted", "AbortError"));
              }
            });
          }
        });
        return new Response(stream, { status: 200, headers: { "Content-Type": "application/x-ndjson" } });
      }
      return jsonResponse({}, 404);
    };
  }, scenario);
}

function finalAnswer(scenario: "completed" | "safe_noop") {
  return scenario === "safe_noop"
    ? "已查看当前行程；本轮无需修改，原有安排保持不变。"
    : "已将第 2 天下半天调整为更顺路的安排。";
}

async function captureChatPane(page: Page, path: string) {
  const pane = page.locator("#workspace-pane-agent");
  await expect(pane).toBeVisible();
  await pane.screenshot({ path });
}

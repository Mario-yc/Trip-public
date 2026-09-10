import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, test } from "vitest";
import { AgentReasoningProgress, aggregateStatuses } from "../../src/components/agent/AgentReasoningProgress";
import type { AgentReasoningStatus } from "../../src/services/apiClient";

const statuses: AgentReasoningStatus[] = [
  {
    messageType: "reasoning_status",
    id: "reasoning_1",
    sequence: 1,
    runId: "run_reasoning",
    semanticKey: "request_understanding",
    phase: "understanding",
    status: "completed",
    summary: "正在理解用户需求",
    detail: null,
    sourceEventType: "normalize_request",
    sessionId: "sess_reasoning",
    turnId: "turn_reasoning",
    timestamp: "2026-08-20T00:00:00Z"
  },
  {
    messageType: "reasoning_status",
    id: "reasoning_2",
    sequence: 2,
    runId: "run_reasoning",
    semanticKey: "poi_discovery",
    phase: "tool",
    status: "running",
    summary: "已找到 3 个候选，正在比较",
    detail: "正在从高德候选中排除距离过远的地点。",
    sourceEventType: "collect_candidates",
    sessionId: "sess_reasoning",
    turnId: "turn_reasoning",
    timestamp: "2026-08-20T00:00:01Z"
  }
];

afterEach(cleanup);

describe("AgentReasoningProgress", () => {
  test("renders only the latest fact without creating an avatar or chat row", () => {
    render(<AgentReasoningProgress live statuses={statuses} />);

    const region = screen.getByLabelText("Agent 规划进度");
    expect(region.getAttribute("data-message-type")).toBe("reasoning_status");
    expect(within(region).queryByText("正在理解用户需求")).toBeNull();
    expect(within(region).getByText("已找到 3 个候选，正在比较")).toBeTruthy();
    expect(within(region).queryByText("最终答案")).toBeNull();
    expect(region.querySelector(".reasoning-status-spinner")).toBeTruthy();
    expect(within(region).queryByText("正在从高德候选中排除距离过远的地点。")).toBeNull();
    expect(region.querySelector(".bot-avatar")).toBeNull();
    expect(region.classList.contains("chat-row")).toBe(false);
    expect(within(region).queryByText(/个阶段/)).toBeNull();
  });

  test("completed history remains a single non-interactive terminal summary", () => {
    const completed = statuses.map((item) => ({ ...item, status: "completed" as const }));
    render(<AgentReasoningProgress live={false} statuses={completed} />);

    const region = screen.getByLabelText("Agent 规划进度");
    expect(within(region).getByText("规划用时 1 秒 · 已找到 3 个候选，正在比较")).toBeTruthy();
    expect(within(region).queryByRole("button")).toBeNull();
    expect(within(region).queryByText("正在理解用户需求")).toBeNull();
  });

  test("deduplicates replay and rejects late state regression by run and semantic key", () => {
    const projected = aggregateStatuses([
      { ...statuses[1], status: "running", sequence: 3, latestSequence: 3 },
      { ...statuses[1], status: "completed", sequence: 4, latestSequence: 4, summary: "已比较 3 个候选" },
      { ...statuses[1], status: "completed", sequence: 4, latestSequence: 4, summary: "已比较 3 个候选" },
      { ...statuses[1], status: "running", sequence: 5, latestSequence: 2, summary: "正在比较旧候选" }
    ]);

    expect(projected).toHaveLength(1);
    expect(projected[0].status).toBe("completed");
    expect(projected[0].summary).toBe("已比较 3 个候选");
  });
});

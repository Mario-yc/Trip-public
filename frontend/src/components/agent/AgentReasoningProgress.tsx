import { useMemo } from "react";
import { LoaderCircle } from "lucide-react";

import type { AgentReasoningStatus } from "../../services/apiClient";

type Props = {
  statuses: AgentReasoningStatus[];
  live: boolean;
  elapsedMs?: number;
};

export function AgentReasoningProgress({ statuses, live, elapsedMs = 0 }: Props) {
  const ordered = useMemo(() => aggregateStatuses(statuses), [statuses]);
  const latest = ordered[ordered.length - 1];
  const runId = latest?.runId || latest?.turnId || "agent-run";

  if (!ordered.length) {
    return null;
  }
  const current = ordered[ordered.length - 1]!;
  const active = live && current.status === "running";
  const durationMs = Math.max(elapsedMs, ...ordered.map((item) => item.elapsedMs ?? 0));
  const durationSeconds = Math.max(1, Math.round(durationMs / 1000));
  const stateLabel = active
    ? current.summary
    : current.status === "cancelled"
      ? `已停止 · ${current.summary}`
      : current.status === "failed"
        ? `处理用时 ${durationSeconds} 秒 · ${current.summary}`
        : `规划用时 ${durationSeconds} 秒 · ${current.summary}`;

  return (
    <section
      aria-label="Agent 规划进度"
      className={`reasoning-status-disclosure ${live ? "live" : "history"} ${current.status}`}
      data-message-type="reasoning_status"
      data-run-id={runId}
    >
      <div className="reasoning-status-trigger">
        <LoaderCircle aria-hidden="true" className={`reasoning-status-spinner ${active ? "active" : ""}`} size={15} />
        <strong aria-live={active ? "polite" : undefined}>{stateLabel}</strong>
      </div>
    </section>
  );
}

export function aggregateStatuses(statuses: AgentReasoningStatus[]): AgentReasoningStatus[] {
  const byKey = new Map<string, AgentReasoningStatus>();
  for (const incoming of [...statuses].sort((left, right) => left.sequence - right.sequence)) {
    const key = reasoningAggregationKey(incoming);
    const current = byKey.get(key);
    if (current && latestSequence(incoming) <= latestSequence(current)) {
      continue;
    }
    if (current && !transitionAllowed(current.status, incoming.status)) {
      continue;
    }
    byKey.set(key, incoming);
  }
  return [...byKey.values()].sort((left, right) => left.sequence - right.sequence).slice(-5);
}

function reasoningAggregationKey(status: AgentReasoningStatus): string {
  const runKey = status.runId || status.sessionId || "legacy";
  if (status.semanticKey) {
    return `${runKey}:${status.semanticKey}`;
  }
  // Older persisted events did not carry semanticKey. Dedupe their cleaned
  // user-visible summary inside the run so a reconnect cannot create a second
  // disclosure item solely because an event was assigned a different id.
  const summaryKey = status.summary.trim().replace(/\s+/g, " ").toLocaleLowerCase();
  return `${runKey}:legacy:${summaryKey || status.id}`;
}

function latestSequence(status: AgentReasoningStatus): number {
  return status.latestSequence ?? status.sequence;
}

function transitionAllowed(
  previous: AgentReasoningStatus["status"],
  incoming: AgentReasoningStatus["status"]
): boolean {
  if (previous === incoming) {
    return true;
  }
  if (previous === "running") {
    return ["completed", "fallback", "failed", "cancelled"].includes(incoming);
  }
  if (previous === "fallback") {
    return incoming === "completed";
  }
  return false;
}

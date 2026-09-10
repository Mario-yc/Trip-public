import type { LocalReplanSuggestion, PlanningRun } from "../../services/apiClient";

export type PlanningStepStatus = "waiting" | "querying" | "completed" | "fallback" | "failed";

export type PlanningStep = {
  id: string;
  label: string;
  status: PlanningStepStatus;
  source: string;
  updatedAt: string;
  confidence?: number;
  fallbackNote?: string;
};

export type AgentPlanningProcessModel = {
  userRequirement: string;
  preferenceSummary: string;
  constraints: Array<{ label: string; value: string }>;
  steps: PlanningStep[];
  resultSummary: string;
};

type AgentPlanningProcessProps = {
  process: AgentPlanningProcessModel | null;
  isStreaming: boolean;
  planningRun?: PlanningRun | null;
  isApplyingSuggestion?: boolean;
  onApplySuggestion?: (suggestion: LocalReplanSuggestion) => void;
};

const STATUS_LABELS: Record<PlanningStepStatus, string> = {
  waiting: "等待中",
  querying: "查询中",
  completed: "已完成",
  fallback: "已降级",
  failed: "失败"
};

export function AgentPlanningProcess({
  process,
  isStreaming,
  planningRun,
  isApplyingSuggestion = false,
  onApplySuggestion
}: AgentPlanningProcessProps) {
  if (!process) {
    return null;
  }
  const feasibility = planningRun?.feasibilityReport;
  const toolCalls = planningRun?.toolCalls ?? [];
  const sourceAssessments = planningRun?.sourceAssessments ?? [];
  const qualityReports = toolCalls.map(qualityContractFromToolCall).filter((report): report is QualityContractPreview => Boolean(report));
  const latestQualityReport = qualityReports.length ? qualityReports[qualityReports.length - 1] : null;

  return (
    <section className="agent-planning-process" aria-label="Agent planning process">
      <details>
      <summary className="planning-header">
        <span>Agent 规划过程</span>
        {isStreaming ? <em>执行中</em> : <em>已完成</em>}
      </summary>
      <div className="planning-summary-grid">
        <section>
          <h3>当前理解到的用户需求</h3>
          <p>{process.userRequirement}</p>
        </section>
        <section>
          <h3>使用中的偏好摘要</h3>
          <p>{process.preferenceSummary}</p>
        </section>
      </div>
      <section className="planning-constraints" aria-label="Planning constraints">
        <h3>约束条件</h3>
        <dl>
          {process.constraints.map((constraint) => (
            <div key={constraint.label}>
              <dt>{constraint.label}</dt>
              <dd>{constraint.value}</dd>
            </div>
          ))}
        </dl>
      </section>
      <ol className="planning-step-list" aria-label="Tool execution steps">
        {(toolCalls.length ? toolCalls.map(toolCallToStep) : process.steps).map((step) => (
          <li className={`planning-step ${normalizeStepStatus(step.status)}`} key={step.id}>
            <div>
              <strong>{step.label}</strong>
              <span>{STATUS_LABELS[normalizeStepStatus(step.status)]}</span>
            </div>
            <p>
              来源：{friendlySourceLabel(step.source)} · 更新时间：{step.updatedAt}
              {typeof step.confidence === "number" ? ` · 置信度 ${(step.confidence * 100).toFixed(0)}%` : ""}
            </p>
            {step.fallbackNote ? <small>{friendlyUserMessage(step.fallbackNote)}</small> : null}
          </li>
        ))}
      </ol>
      {planningRun?.understoodRequirements?.clarificationQuestions?.length ? (
        <section className="planning-check-card" aria-label="Clarification questions">
          <h3>需要确认的信息</h3>
          <ul>
            {planningRun.understoodRequirements.clarificationQuestions.slice(0, 3).map((question) => (
              <li key={question}>{question}</li>
            ))}
          </ul>
        </section>
      ) : null}
      {sourceAssessments.length ? (
        <section className="planning-check-card" aria-label="Source credibility assessment">
          <h3>来源可信度检查</h3>
          <ul>
            {sourceAssessments.slice(0, 4).map((source) => (
              <li key={`${source.sourceName}-${source.sourceUrl ?? source.credibilityRank}`}>
                <strong>
                  {source.credibilityLabel} · {source.sourceName}
                </strong>
                <span>
                  置信度 {(source.confidence * 100).toFixed(0)}%
                  {source.fallbackUsed ? " · 已降级" : ""}
                  {source.conflictDetected ? " · 存在冲突" : ""}
                </span>
                {source.conflictReason ? <span>{friendlyUserMessage(source.conflictReason)}</span> : null}
                <span>{friendlyUserMessage(source.recommendation)}</span>
                {source.sourceUrl ? (
                  <a href={source.sourceUrl} rel="noreferrer" target="_blank">
                    查看来源
                  </a>
                ) : null}
              </li>
            ))}
          </ul>
        </section>
      ) : null}
      {latestQualityReport ? (
        <section
          className={`planning-check-card ${latestQualityReport.canCreateActiveVersion ? "low" : "high"}`}
          aria-label="Itinerary quality gate"
        >
          <div className="planning-check-header">
            <h3>时间轴质量门槛</h3>
            <span>{latestQualityReport.canCreateActiveVersion ? "可创建正式时间轴" : "未完成正式时间轴"}</span>
          </div>
          <p>{qualityStatusLabel(latestQualityReport.qualityStatus)}</p>
          {latestQualityReport.hardFailures.length ? (
            <ul>
              {latestQualityReport.hardFailures.slice(0, 4).map((failure) => (
                <li key={failure}>{qualityFailureLabel(failure)}</li>
              ))}
            </ul>
          ) : null}
          {!latestQualityReport.canCreateActiveVersion ? (
            <p>尚未生成完整正式时间轴：仍有核心地点待自动补全。可继续自动补全、降低严格度生成草案或手动选择。</p>
          ) : null}
        </section>
      ) : null}
      {feasibility ? (
        <section className={`planning-check-card ${feasibility.riskLevel}`} aria-label="Itinerary feasibility check">
          <div className="planning-check-header">
            <h3>行程可行性检查</h3>
            <span>{feasibility.score}/100 · {riskLabel(feasibility.riskLevel)}</span>
          </div>
          <p>{feasibility.preferenceAlignment}</p>
          {feasibility.issues.length ? (
            <ul>
              {feasibility.issues.slice(0, 3).map((issue) => (
                <li key={`${issue.code}-${issue.affectedSegmentId ?? issue.dimension}`}>
                  <strong>{issue.message}</strong>
                  <span>{issue.recommendation}</span>
                </li>
              ))}
            </ul>
          ) : (
            <p>未发现阻塞性风险，仍建议出行前确认官方开放和预约信息。</p>
          )}
        </section>
      ) : null}
      {feasibility?.localReplanSuggestions?.length ? (
        <section className="planning-check-card" aria-label="Local replanning suggestions">
          <h3>局部优化建议</h3>
          <ul>
            {feasibility.localReplanSuggestions.slice(0, 5).map((suggestion) => (
              <li key={suggestion.id}>
                <strong>{suggestion.summary}</strong>
                <span>{suggestion.rationale} · 等待确认后再应用</span>
                {suggestion.operations.length ? (
                  <button
                    type="button"
                    className="planning-suggestion-action"
                    disabled={isStreaming || isApplyingSuggestion}
                    onClick={() => onApplySuggestion?.(suggestion)}
                  >
                    {isApplyingSuggestion ? "应用中..." : "确认应用"}
                  </button>
                ) : null}
              </li>
            ))}
          </ul>
        </section>
      ) : null}
      <p className="planning-result-summary" aria-live="polite">
        {planningRun?.finalSummary || process.resultSummary}
        {isStreaming ? <span className="stream-cursor" aria-hidden="true">|</span> : null}
      </p>
      </details>
    </section>
  );
}

type QualityContractPreview = {
  canCreateActiveVersion: boolean;
  qualityStatus: string;
  hardFailures: string[];
};

function qualityContractFromToolCall(toolCall: PlanningRun["toolCalls"][number]): QualityContractPreview | null {
  const metadata = toolCall.metadata;
  if (!isRecord(metadata)) {
    return null;
  }
  const resultPreview = metadata.resultPreview;
  if (!isRecord(resultPreview)) {
    return null;
  }
  const direct = resultPreview.qualityContract;
  const finalization = resultPreview.finalization;
  const nested = isRecord(finalization) ? finalization.qualityContract : null;
  const report = isRecord(direct) ? direct : isRecord(nested) ? nested : null;
  if (!report) {
    return null;
  }
  return {
    canCreateActiveVersion: report.canCreateActiveVersion === true,
    qualityStatus: typeof report.qualityStatus === "string" ? report.qualityStatus : "",
    hardFailures: Array.isArray(report.hardFailures)
      ? report.hardFailures.map((item) => String(item)).filter(Boolean)
      : []
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function qualityStatusLabel(status: string) {
  if (status === "pass") {
    return "已满足真实地点、覆盖完整度和基础路线质量要求。";
  }
  if (status === "draft_needs_completion") {
    return "还有必选地点或路线质量未达标，本轮仅保留规划预览。";
  }
  return status || "质量状态待确认。";
}

function qualityFailureLabel(failure: string) {
  const labels: Record<string, string> = {
    required_placeholder_segments_present: "存在必选地点占位符",
    weak_poi_selected: "存在弱实体地点",
    required_slots_missing: "必选地点未全部落地",
    route_leg_distance_too_long: "单段路线距离过长",
    day_route_distance_too_long: "单日路线距离过长",
    trip_route_distance_too_long: "两日总路线距离过长"
  };
  return labels[failure] ?? failure;
}

function toolCallToStep(toolCall: PlanningRun["toolCalls"][number]): PlanningStep {
  return {
    id: toolCall.id,
    label: toolCall.toolName,
    status: normalizeStepStatus(toolCall.status),
    source: toolCall.sourceName || toolCall.providerName || "服务",
    updatedAt: formatPlanningTime(toolCall.queriedAt),
    confidence: toolCall.confidence,
    fallbackNote: toolCall.fallbackUsed
      ? toolCall.userVisibleCaveat || toolCall.failureReason || "服务已降级，需用户确认关键结果。"
      : toolCall.summary
  };
}

function normalizeStepStatus(status: string): PlanningStepStatus {
  if (status === "waiting" || status === "querying" || status === "completed" || status === "fallback" || status === "failed") {
    return status;
  }
  return status === "available" ? "completed" : "fallback";
}

function formatPlanningTime(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value || "刚刚";
  }
  return date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
}

function riskLabel(riskLevel: string) {
  const labels: Record<string, string> = {
    high: "高风险",
    medium: "中风险",
    low: "低风险",
    risky: "有风险",
    ideal: "适宜",
    neutral: "影响较低",
    unknown: "待判断",
    unavailable: "不可用",
    bad_weather: "天气风险"
  };
  return labels[riskLevel] ?? riskLevel;
}

function friendlySourceLabel(source: string) {
  if (/amap|高德/i.test(source)) {
    return "高德地图";
  }
  if (/weather/i.test(source)) {
    return "天气服务";
  }
  if (/ticket|reservation|web-search|bocha/i.test(source)) {
    return "公开来源查询";
  }
  if (/provider|mock|fallback/i.test(source)) {
    return "规划服务";
  }
  return source || "规划服务";
}

function friendlyUserMessage(message: string) {
  if (!message) {
    return "";
  }
  if (/KEY|TOKEN|SECRET|API|PROVIDER|provider|mock|fallback|AMAP|WEB_SERVICE|configured|not configured|bocha/i.test(message)) {
    return "相关服务暂时不可用，当前结果可能不完整；请稍后重新查询或检查服务配置。";
  }
  return message.replace(/\bfallback\b/gi, "降级");
}

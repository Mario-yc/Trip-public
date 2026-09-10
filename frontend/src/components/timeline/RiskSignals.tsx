import { useState } from "react";
import { ItineraryPlan } from "../../services/apiClient";

type RiskSignalsProps = {
  plan: ItineraryPlan;
};

export function RiskSignals({ plan }: RiskSignalsProps) {
  const weather = plan.weatherSignals[0];
  const traffic = plan.trafficCrowdingSignals[0];
  const poiRiskAlerts = plan.poiRiskAlerts ?? [];
  const [collapsed, setCollapsed] = useState(true);
  const weatherSummary = weather ? `${weather.date ? `${formatWeatherDate(weather.date)} · ` : ""}${weather.dailySummary}` : "待 Agent 查询";
  const riskSummary = traffic ? trafficRiskSummary(traffic) : "待 Agent 查询";

  return (
    <section aria-label="Weather and crowding signals" className="risk-signals">
      <div className="risk-signals-header">
        <div>
          <h3>风险提示</h3>
          <p className="risk-signals-summary">天气：{weatherSummary} · 景点风险：{riskSummary}</p>
        </div>
        <button
          aria-expanded={!collapsed}
          className="risk-signals-toggle"
          onClick={() => setCollapsed((current) => !current)}
          type="button"
        >
          {collapsed ? "展开风险" : "收起风险"}
        </button>
      </div>
      {collapsed ? null : (
        <>
          <details className="risk-reminder">
            <summary>
              <strong>天气</strong>
              <span>{weather?.dailySummary ?? "待查询"}</span>
            </summary>
            <p>{weather?.purposeImpactReason ?? "暂无天气影响说明"}</p>
            <p>
              来源：{weather ? sourceLabel(weather.providerName || weather.source, "天气服务") : "待 Agent 查询"}
              {weather?.date ? ` · 天气日期 ${formatWeatherDate(weather.date)}` : ""}
              {weather?.queriedAt ? ` · 查询 ${formatQueryTime(weather.queriedAt)}` : ""}
            </p>
            {weather ? (
              <p>
                状态：{statusLabel(weather.dataStatus)} · 风险等级：{weatherRiskLevelLabel(weather)}
                {typeof weather.confidence === "number" ? ` · 置信度 ${Math.round(weather.confidence * 100)}%` : ""}
                {weather.fallbackUsed && weather.dataStatus !== "fallback" ? " · 已降级" : ""}
              </p>
            ) : null}
            {weather?.fallbackUsed ? <p className="risk-data-caveat">天气查询已降级：{friendlyFailureReason(weather.failureReason)}</p> : null}
            {weather?.failureReason && !weather.fallbackUsed ? (
              <p className="risk-data-caveat">天气查询失败：{friendlyFailureReason(weather.failureReason)}</p>
            ) : null}
            {weather?.userVisibleCaveat ? <p className="risk-data-caveat">{friendlyWeatherCaveat(weather.userVisibleCaveat)}</p> : null}
          </details>
          <details className="risk-reminder">
            <summary>
              <strong>拥挤</strong>
              <span>{traffic ? trafficRiskSummary(traffic) : "待估算"}</span>
            </summary>
            <p>
              来源：{traffic ? sourceLabel(traffic.source, "交通服务") : "待 Agent 查询"}
              {traffic?.queriedAt ? ` · 查询 ${formatQueryTime(traffic.queriedAt)}` : ""}
            </p>
          </details>
          {weather?.source?.includes("mock") || traffic?.source?.includes("mock") ? (
            <p className="risk-data-caveat">真实天气/风险数据待接入，当前结果不作为完整风险判断。</p>
          ) : null}
          <div className="poi-risk-alerts" aria-label="POI risk search alerts">
            <h4>景点风险搜索</h4>
            {poiRiskAlerts.length ? (
              poiRiskAlerts.slice(0, 4).map((alert) => {
                const visibleSources = alert.sources.filter((source) => !source.type && (source.title || source.url || source.snippet));
                const providerSummary = providerDiagnosticsSummary(alert);
                const status = poiRiskStatusLabel(alert);
                const sourceStats = riskSourceStatsSummary(alert);
                return (
                  <details className={`poi-risk-card ${alert.status}`} key={alert.id}>
                    <summary>
                      <strong>{alert.poiName}</strong>
                      <span>{status}</span>
                    </summary>
                    <p>{alert.summary}</p>
                    <p>下一步建议：{poiRiskNextAction(alert)}</p>
                    <p>
                      来源：
                      {alert.sourceUrl ? (
                        <a href={alert.sourceUrl} rel="noreferrer" target="_blank">
                          {alert.sourceName}
                        </a>
                      ) : (
                        alert.sourceName
                      )}
                      {alert.queriedAt ? ` · 查询 ${formatQueryTime(alert.queriedAt)}` : ""}
                      {typeof alert.confidence === "number" ? ` · 置信度 ${Math.round(alert.confidence * 100)}%` : ""}
                    </p>
                    {sourceStats ? <p>来源统计：{sourceStats}</p> : null}
                    {providerSummary ? <p className="risk-data-caveat">联网搜索：{providerSummary}</p> : null}
                    {alert.failureReason ? (
                      <p className="risk-data-caveat">
                        {alert.status === "degraded" ? "风险判断提示" : "搜索提示"}：{friendlySearchFailureReason(alert.failureReason)}
                      </p>
                    ) : null}
                    {alert.userVisibleCaveat ? <p className="risk-data-caveat">{friendlySearchCaveat(alert.userVisibleCaveat)}</p> : null}
                    {visibleSources.length ? (
                      <details className="risk-source-links">
                        <summary>查看来源 {visibleSources.length}</summary>
                        <ul>
                          {visibleSources.map((source, index) => (
                            <li key={`${alert.id}-${source.url ?? source.title ?? index}`}>
                              {source.url ? (
                                <a href={source.url} rel="noreferrer" target="_blank">
                                  {source.title ?? source.url}
                                </a>
                              ) : (
                                <span>{source.title}</span>
                              )}
                              {source.credibilityRank ? <small>{credibilityLabel(String(source.credibilityRank))}</small> : null}
                            </li>
                          ))}
                        </ul>
                      </details>
                    ) : null}
                    <RiskDebugDetails alert={alert} />
                  </details>
                );
              })
            ) : (
              <p>待 Agent 联网搜索近期景点风险。</p>
            )}
          </div>
        </>
      )}
    </section>
  );
}

function RiskDebugDetails({ alert }: { alert: NonNullable<ItineraryPlan["poiRiskAlerts"]>[number] }) {
  const [open, setOpen] = useState(false);
  const riskDiagnostics = riskSearchDiagnostics(alert);
  const providerDiagnostics = providerDiagnosticsSource(alert);
  const debugSources = alert.sources.filter((source) => !source.type && (source.title || source.url || source.snippet));
  if (!riskDiagnostics && !providerDiagnostics && !debugSources.length) {
    return null;
  }
  return (
    <details className="risk-debug-details">
      <summary onClick={() => setOpen(true)}>Debug 详情</summary>
      {open ? (
        <div>
          {riskDiagnostics ? (
            <p>
              查询：{String(riskDiagnostics.query || "未记录")} · 状态：{String(riskDiagnostics.riskStatusReason || "unknown")}
            </p>
          ) : null}
          {debugSources.length ? (
            <ul>
              {debugSources.map((source, index) => (
                <li key={`debug-${alert.id}-${source.url ?? source.title ?? index}`}>
                  <strong>{source.title ?? source.url ?? "未命名来源"}</strong>
                  {source.url ? <span> · {source.url}</span> : null}
                  {source.snippet ? <small>{source.snippet}</small> : null}
                </li>
              ))}
            </ul>
          ) : null}
          {providerDiagnostics ? <pre>{JSON.stringify(providerDiagnostics, null, 2)}</pre> : null}
        </div>
      ) : null}
    </details>
  );
}

function sourceLabel(source: string | undefined, fallback: string) {
  if (!source) {
    return fallback;
  }
  if (source.includes("高德")) {
    return source;
  }
  if (source.includes("amap")) {
    return "高德地图";
  }
  if (source.includes("mock")) {
    return `${fallback}（真实数据待接入）`;
  }
  return fallback;
}

function riskLevelLabel(level: string | undefined) {
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
  return labels[level ?? ""] ?? level ?? "待判断";
}

function weatherRiskLevelLabel(weather: NonNullable<ItineraryPlan["weatherSignals"][number]>) {
  if (isLowLikeRisk(weather.riskLevel) && !hasReliableWeatherRisk(weather)) {
    return "风险待核验";
  }
  return riskLevelLabel(weather.riskLevel);
}

function trafficRiskSummary(traffic: NonNullable<ItineraryPlan["trafficCrowdingSignals"][number]>) {
  const label = crowdingRiskLabel(traffic);
  return label === "风险待核验" ? "风险待核验" : `${label} · ${traffic.recommendedDepartureAdjustment}`;
}

function crowdingRiskLabel(traffic: NonNullable<ItineraryPlan["trafficCrowdingSignals"][number]>) {
  if (isLowLikeRisk(traffic.crowdingLevel) && !hasReliableTrafficRisk(traffic)) {
    return "风险待核验";
  }
  return riskLevelLabel(traffic.crowdingLevel);
}

function isLowLikeRisk(level: string | undefined) {
  return ["low", "ideal", "neutral"].includes(String(level || "").toLowerCase());
}

function hasReliableWeatherRisk(weather: NonNullable<ItineraryPlan["weatherSignals"][number]>) {
  const status = String(weather.dataStatus || "").toLowerCase();
  const source = `${weather.source || ""} ${weather.providerName || ""}`.toLowerCase();
  if (weather.fallbackUsed || weather.failureReason) {
    return false;
  }
  if (!status || ["fallback", "unavailable", "pending", "not_checked", "not_required"].includes(status)) {
    return false;
  }
  return !/(mock|unavailable|not configured|provider unavailable)/i.test(source);
}

function hasReliableTrafficRisk(traffic: NonNullable<ItineraryPlan["trafficCrowdingSignals"][number]>) {
  const source = String(traffic.source || "").toLowerCase();
  return Boolean(traffic.realDataAvailable) && !/(mock|unavailable|not configured|provider unavailable)/i.test(source);
}

function statusLabel(status: string | undefined) {
  const labels: Record<string, string> = {
    available: "已查询",
    degraded: "部分数据可用",
    unavailable: "不可用",
    fallback: "已降级",
    pending: "待核验",
    not_checked: "未查询",
    not_required: "无需实时风险"
  };
  return labels[status ?? ""] ?? "待查询";
}

function poiRiskStatusLabel(alert: NonNullable<ItineraryPlan["poiRiskAlerts"]>[number]) {
  if (poiRiskNeedsVerification(alert)) {
    return "风险待核验";
  }
  return statusLabel(alert.status);
}

function poiRiskNeedsVerification(alert: NonNullable<ItineraryPlan["poiRiskAlerts"]>[number]) {
  const diagnostics = riskSearchDiagnostics(alert);
  const acceptedCount = numericDiagnostic(diagnostics?.acceptedSourceCount);
  const reason = String(diagnostics?.riskStatusReason || alert.failureReason || "");
  const status = String(alert.status || "").toLowerCase();
  const noReliableSourceReasons = new Set([
    "search_provider_unavailable",
    "search_no_results",
    "search_results_all_stale",
    "search_results_low_credibility",
    "all_web_search_providers_failed_or_empty"
  ]);
  if (noReliableSourceReasons.has(reason)) {
    return true;
  }
  if ((status === "degraded" || status === "unavailable") && acceptedCount === 0) {
    return true;
  }
  if (status === "unavailable" && !alert.sourceUrl && (acceptedCount === undefined || acceptedCount <= 0)) {
    return true;
  }
  return false;
}

function poiRiskNextAction(alert: NonNullable<ItineraryPlan["poiRiskAlerts"]>[number]) {
  if (poiRiskNeedsVerification(alert)) {
    return "出行前核对景区或场馆官方公告。";
  }
  if (String(alert.status || "").toLowerCase() === "degraded") {
    return "保留当前提醒，并在出行前复核官方来源。";
  }
  return "按当前提醒安排，临近出行再复核一次。";
}

function riskSourceStatsSummary(alert: NonNullable<ItineraryPlan["poiRiskAlerts"]>[number]) {
  const diagnostics = riskSearchDiagnostics(alert);
  const visibleSources = alert.sources.filter((source) => !source.type && (source.title || source.url || source.snippet));
  const accepted = numericDiagnostic(diagnostics?.acceptedSourceCount) ?? visibleSources.length;
  const official =
    numericDiagnostic(diagnostics?.officialSourceCount) ??
    visibleSources.filter((source) => String(source.credibilityRank || "").toLowerCase() === "official").length;
  const sourceCount = numericDiagnostic(diagnostics?.sourceCount) ?? accepted;
  const stale = numericDiagnostic(diagnostics?.rejectedStaleSourceCount) ?? 0;
  const rejected = Math.max(0, sourceCount - accepted);
  if (!diagnostics && !visibleSources.length) {
    return "";
  }
  return `已采纳来源 ${accepted} 条；官方来源 ${official} 条；已拒绝过期/低相关 ${rejected} 条${stale ? `（过期 ${stale} 条）` : ""}`;
}

function riskSearchDiagnostics(alert: NonNullable<ItineraryPlan["poiRiskAlerts"]>[number]) {
  return alert.sources.find((source) => source.type === "riskSearchDiagnostics");
}

function providerDiagnosticsSource(alert: NonNullable<ItineraryPlan["poiRiskAlerts"]>[number]) {
  return alert.sources.find((source) => source.type === "webSearchProviderDiagnostics");
}

function numericDiagnostic(value: unknown) {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function credibilityLabel(rank: string) {
  const labels: Record<string, string> = {
    official: "官方来源",
    ota_aggregator: "预约/票务平台",
    guide: "攻略来源",
    social: "社交来源",
    search: "搜索结果",
    unknown: "来源待判断"
  };
  return labels[rank] ?? rank;
}

function friendlyFailureReason(reason: string | null | undefined) {
  if (!reason) {
    return "天气服务暂时不可用，请稍后重新查询。";
  }
  if (/KEY|TOKEN|SECRET|API|PROVIDER|AMAP|WEB_SERVICE|configured|not configured/i.test(reason)) {
    return "天气服务暂时不可用，请稍后重新查询或检查服务配置。";
  }
  return reason;
}

function friendlySearchFailureReason(reason: string | null | undefined) {
  if (!reason) {
    return "搜索状态待确认。";
  }
  const labels: Record<string, string> = {
    search_provider_unavailable: "搜索供应商未配置或暂不可用，当前无法自动核验官方公告。",
    search_no_results: "未找到可用公开搜索结果。",
    search_results_all_stale: "找到公开结果，但未满足当前出行日期要求。",
    search_results_low_credibility: "找到公开结果，但未满足官方来源或可信度要求。",
    search_success_degraded: "部分搜索供应商失败或跳过，当前风险判断仍需核对官方公告。",
    all_web_search_providers_failed_or_empty: "所有搜索供应商均失败、跳过或未返回可用结果。"
  };
  if (labels[reason]) {
    return labels[reason];
  }
  if (/KEY|TOKEN|SECRET|API|PROVIDER|configured|not configured/i.test(reason)) {
    return "搜索供应商未配置，当前无法自动核验官方公告。";
  }
  return reason;
}

function friendlyWeatherCaveat(caveat: string) {
  if (/KEY|TOKEN|SECRET|API|PROVIDER|provider|mock|fallback|AMAP|WEB_SERVICE|configured|not configured/i.test(caveat)) {
    return "天气服务暂时不可用，当前天气风险判断不完整；出行前请重新查询真实天气。";
  }
  return caveat;
}

function friendlySearchCaveat(caveat: string) {
  if (/KEY|TOKEN|SECRET|API|PROVIDER|provider|configured|not configured/i.test(caveat)) {
    return "搜索供应商暂不可用，当前风险判断不完整；出行前请核对官方公告。";
  }
  return caveat;
}

function providerDiagnosticsSummary(alert: NonNullable<ItineraryPlan["poiRiskAlerts"]>[number]) {
  const diagnosticsSource = providerDiagnosticsSource(alert);
  const riskDiagnostics = riskSearchDiagnostics(alert);
  const providerDiagnostics = Array.isArray(diagnosticsSource?.providerDiagnostics) ? diagnosticsSource.providerDiagnostics : [];
  if (!providerDiagnostics.length) {
    return "";
  }
  const allSkippedMissingConfig = providerDiagnostics.every(
    (item) => String(item.status) === "skipped" && String(item.reason) === "skipped_missing_config"
  );
  if (allSkippedMissingConfig) {
    return "搜索供应商未配置，当前无法自动核验官方公告。";
  }
  const parts = providerDiagnostics.slice(0, 6).map((item) => {
    const provider = providerDisplayName(String(item.providerName || ""));
    const status = String(item.status || "");
    const reason = String(item.reason || "");
    const resultCount = typeof item.resultCount === "number" ? item.resultCount : 0;
    if (status === "success") {
      return `${provider} 成功 ${resultCount} 条`;
    }
    if (status === "skipped" && reason === "skipped_missing_config") {
      return `${provider} 未配置`;
    }
    if (status === "skipped") {
      return `${provider} 跳过`;
    }
    if (status === "no_results" || reason === "no_usable_results") {
      return `${provider} 无可用结果`;
    }
    if (reason === "timeout") {
      return `${provider} 超时`;
    }
    if (status === "cache_hit") {
      return `${provider} 命中缓存`;
    }
    return `${provider} 失败`;
  });
  const statusReason = String(riskDiagnostics?.riskStatusReason || alert.failureReason || "");
  if (statusReason === "search_results_all_stale") {
    parts.push("公开结果未满足当前出行日期要求");
  } else if (statusReason === "search_results_low_credibility") {
    parts.push("公开结果未满足官方来源或可信度要求");
  }
  return parts.join("；");
}

function providerDisplayName(providerName: string) {
  const labels: Record<string, string> = {
    "bocha-web-search": "Bocha",
    tavily: "Tavily",
    "brave-web-search": "Brave",
    searxng: "SearXNG",
    "google-cse": "Google CSE",
    "multi-free-search": "Multi-free",
    "baidu-html-search": "Baidu",
    "duckduckgo-html-search": "DuckDuckGo",
    "chained-web-search": "Provider chain"
  };
  return labels[providerName] ?? (providerName || "搜索供应商");
}

function formatQueryTime(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleString("zh-CN", { hour12: false });
}

function formatWeatherDate(value: string) {
  const date = new Date(`${value}T00:00:00`);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleDateString("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit" });
}

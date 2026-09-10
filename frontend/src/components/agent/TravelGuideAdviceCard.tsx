import type { TravelGuideAdvice } from "../../services/apiClient";
import { safeExternalHttpUrl } from "../../services/safeExternalUrl";

type Props = {
  advice: TravelGuideAdvice;
};

const credibilityLabels: Record<string, string> = {
  official: "官方来源",
  ota: "旅行平台",
  map: "地图来源",
  guide: "攻略来源",
  search: "搜索来源",
  unknown: "来源待识别"
};

function formatQueriedAt(value?: string) {
  if (!value) {
    return "";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "";
  }
  return date.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit"
  });
}

export function TravelGuideAdviceCard({ advice }: Props) {
  const relevanceValidated = Boolean(advice.relevanceFilter);
  const recommendations = relevanceValidated && Array.isArray(advice.recommendations) ? advice.recommendations : [];
  const cautions = relevanceValidated && Array.isArray(advice.cautions) ? advice.cautions : [];
  const sourceRefs = (relevanceValidated && Array.isArray(advice.sourceRefs) ? advice.sourceRefs : []).flatMap(
    (source) => {
      const url = safeExternalHttpUrl(source.url);
      return url ? [{ ...source, url }] : [];
    }
  );
  const attemptedProviders = Array.isArray(advice.attemptedProviders) ? advice.attemptedProviders : [];
  const status = advice.status ?? (recommendations.length ? "completed" : "no_results");
  const acceptedCount = advice.relevanceFilter?.acceptedResultCount ?? recommendations.length;
  const rejectedCount = advice.relevanceFilter?.rejectedResultCount ?? 0;
  const statusTitle = !relevanceValidated
    ? "历史攻略结果待重新核验"
    : status === "failed"
      ? "攻略来源暂时不可用"
      : status === "completed"
        ? `找到 ${acceptedCount} 条相关攻略摘要`
        : "没有找到足够相关的攻略";
  const conclusion = advice.conclusion;
  const sourceByRefId = new Map(sourceRefs.map((source) => [source.refId, source]));

  return (
    <section aria-label="普通攻略建议" className={`agent-guide-advice ${status}`}>
      <header className="agent-guide-advice-header">
        <div>
          <strong>普通攻略建议</strong>
          <span>{statusTitle}</span>
        </div>
        <span className="agent-guide-readonly-badge">只读 · 不改行程</span>
      </header>

      {conclusion ? (
        <section aria-label="攻略整理结论" className="agent-guide-conclusion">
          <div className="agent-guide-conclusion-heading">
            <strong>整理结论</strong>
            <span>
              {conclusion.generationMethod === "deepseek_structured_v1" ? "证据归纳 · AI 整理" : "证据规则归纳"}
            </span>
          </div>
          <p>{conclusion.overview}</p>
          {conclusion.takeaways.length ? (
            <ol>
              {conclusion.takeaways.map((item, index) => (
                <li key={`${item.intentType}:${index}`}>
                  <strong>{item.themeLabel}</strong>
                  <span>{item.text}</span>
                  {item.sourceRefIds.length ? (
                    <small>
                      依据：{item.sourceRefIds.map((refId) => sourceByRefId.get(refId)?.title || refId).join("；")}
                    </small>
                  ) : null}
                </li>
              ))}
            </ol>
          ) : null}
          {conclusion.conflicts.length ? (
            <div className="agent-guide-conflicts">
              <strong>证据冲突</strong>
              {conclusion.conflicts.map((item, index) => (
                <p key={`${item.topic}:${index}`}>
                  {item.topic}：{item.summary}
                </p>
              ))}
            </div>
          ) : null}
          {conclusion.missingThemes.length ? (
            <p className="agent-guide-missing">
              仍缺信息：{conclusion.missingThemes.map((item) => item.themeLabel).join("、")}
            </p>
          ) : null}
        </section>
      ) : relevanceValidated && recommendations.length ? (
        <div className="agent-guide-legacy-notice">旧结果尚未生成整理结论，重新搜索可获取。</div>
      ) : null}

      {recommendations.length ? (
        <details className="agent-guide-raw-evidence">
          <summary>来源摘要与原文（{recommendations.length}）</summary>
          <ol className="agent-guide-recommendations">
            {recommendations.map((item, index) => {
              const sourceUrl = safeExternalHttpUrl(item.sourceUrl);
              const queriedAt = formatQueriedAt(item.queriedAt);
              const credibility = item.credibilityRank
                ? (credibilityLabels[item.credibilityRank] ?? "普通网页来源")
                : "普通网页来源";
              return (
                <li key={`${sourceUrl ?? item.title ?? "advice"}:${index}`}>
                  <article>
                    <div className="agent-guide-item-heading">
                      <strong>{item.title || `攻略摘要 ${index + 1}`}</strong>
                      <span>未核验建议</span>
                    </div>
                    <p className="agent-guide-summary">{item.text}</p>
                    <footer>
                      <span>{[item.sourceName, credibility, queriedAt].filter(Boolean).join(" · ")}</span>
                      {sourceUrl ? (
                        <a href={sourceUrl} rel="noreferrer" target="_blank">
                          打开原文
                        </a>
                      ) : null}
                    </footer>
                  </article>
                </li>
              );
            })}
          </ol>
        </details>
      ) : (
        <div className="agent-guide-empty-state">
          <strong>{relevanceValidated ? "本轮未返回可引用内容" : "旧摘要已隐藏"}</strong>
          <p>
            {!relevanceValidated
              ? "这条历史结果生成于新版相关性校验之前，请使用“重新搜索普通攻略”获取当前结果。"
              : status === "failed"
                ? `已尝试 ${attemptedProviders.length} 个搜索来源，但本轮没有得到可安全引用的结果。`
                : rejectedCount
                  ? `已排除 ${rejectedCount} 条与目的地、旅行主题或到访建议不匹配的结果。`
                  : "本轮搜索完成，但没有得到可引用的普通攻略。"}
          </p>
        </div>
      )}

      {cautions.length ? (
        <section aria-label="攻略注意事项" className="agent-guide-cautions">
          <strong>注意事项</strong>
          <ul>
            {cautions.map((item, index) => {
              const sourceUrl = safeExternalHttpUrl(item.sourceUrl);
              return (
                <li key={`${sourceUrl ?? "caution"}:${index}`}>
                  <span>{item.text}</span>
                  {sourceUrl ? (
                    <a href={sourceUrl} rel="noreferrer" target="_blank">
                      查看来源
                    </a>
                  ) : null}
                </li>
              );
            })}
          </ul>
        </section>
      ) : null}

      {sourceRefs.length ? (
        <details className="agent-guide-sources">
          <summary>引用来源索引（{sourceRefs.length}）</summary>
          <ul>
            {sourceRefs.map((source) => {
              const metadata = [
                source.sourceName,
                source.credibilityRank ? (credibilityLabels[source.credibilityRank] ?? "普通网页来源") : "",
                formatQueriedAt(source.queriedAt)
              ]
                .filter(Boolean)
                .join(" · ");
              return (
                <li key={source.refId || source.url}>
                  <a href={source.url} rel="noreferrer" target="_blank">
                    {source.title || source.sourceName || "查看来源"}
                  </a>
                  {metadata ? <span>{metadata}</span> : null}
                </li>
              );
            })}
          </ul>
        </details>
      ) : null}

      <footer className="agent-guide-advice-footer">
        <strong>搜索摘要，不是网页全文</strong>
        <span>
          {relevanceValidated ? advice.caveat : "历史结果不会自动重新解释或写入行程；重新搜索后将使用当前相关性规则。"}
        </span>
      </footer>
    </section>
  );
}

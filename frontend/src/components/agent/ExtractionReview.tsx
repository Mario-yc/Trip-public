type ExtractionReviewProps = {
  cityCandidates: string[];
  poiCandidates: Array<{ name: string; confidence: number; sourceLinks?: string[] }>;
  styleTags: string[];
  budgetClues: string[];
  routeClues: string[];
  confidence: number;
  sourceLinks: string[];
  needsUserConfirmation: boolean;
  providerName: string;
  fallbackUsed: boolean;
  providerFailureReason?: string;
  userVisibleCaveat?: string;
};

export function ExtractionReview({
  cityCandidates,
  poiCandidates,
  styleTags,
  budgetClues,
  routeClues,
  confidence,
  sourceLinks,
  needsUserConfirmation,
  providerName,
  fallbackUsed,
  providerFailureReason,
  userVisibleCaveat
}: ExtractionReviewProps) {
  return (
    <section aria-label="Extraction review">
      <h2>识别结果</h2>
      <p>整体置信度：{Math.round(confidence * 100)}%</p>
      <p>Provider：{providerName}{fallbackUsed ? " · 已使用兜底" : ""}</p>
      {userVisibleCaveat ? <p role="status">{userVisibleCaveat}</p> : null}
      {providerFailureReason ? <p>失败原因：{providerFailureReason}</p> : null}
      <p>{needsUserConfirmation ? "需要用户确认识别结果" : "识别结果可继续编辑"}</p>
      <p>城市：{cityCandidates.join("、") || "待确认"}</p>
      <p>风格：{styleTags.join("、") || "待确认"}</p>
      <p>预算线索：{budgetClues.join("；") || "待确认"}</p>
      <p>路线线索：{routeClues.join("；") || "待确认"}</p>
      <details>
        <summary>来源链接（{sourceLinks.length}）</summary>
        {sourceLinks.length ? (
          <ul>
            {sourceLinks.map((link) => (
              <li key={link}>
                <a href={link} rel="noreferrer" target="_blank">
                  {link}
                </a>
              </li>
            ))}
          </ul>
        ) : (
          <p>暂无外部来源链接</p>
        )}
      </details>
      <h3>候选地点</h3>
      <ul>
        {poiCandidates.map((poi) => (
          <li key={poi.name}>
            {poi.name} · 置信度 {Math.round(poi.confidence * 100)}%
          </li>
        ))}
      </ul>
    </section>
  );
}

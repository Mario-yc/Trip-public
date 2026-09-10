import { TicketLookupResult } from "../../services/apiClient";

type TicketSourceListProps = {
  results: TicketLookupResult[];
};

export function TicketSourceList({ results }: TicketSourceListProps) {
  if (results.length === 0) {
    return <p>预约来源待查询。</p>;
  }

  return (
    <details className="ticket-sources">
      <summary>预约来源（{results.length}）</summary>
      <ul>
        {results.map((result) => (
          <li key={result.id}>
            {hasOfficialReservationEntry(result) ? (
              <a href={result.sourceUrl} rel="noreferrer" target="_blank">
                {result.sourceName}
              </a>
            ) : (
              <span>{result.sourceName || "查询失败/待确认"}</span>
            )}
            <span>
              {rankLabel(result.credibilityRank)} · {reservationStatusLabel(result.status, hasOfficialReservationEntry(result))}
            </span>
            <small>
              查询 {formatTime(result.queriedAt)} · 置信度 {Math.round(result.confidence * 100)}%
              {result.caveat ? ` · ${result.caveat}` : ""}
            </small>
          </li>
        ))}
      </ul>
    </details>
  );
}

function reservationStatusLabel(status: string, hasOfficialEntry: boolean) {
  if (["available", "reservation_required"].includes(status) && !hasOfficialEntry) {
    return "未找到官方入口";
  }
  const labels: Record<string, string> = {
    reservation_required: "需预约",
    available: "官方入口已找到",
    open: "无需预约",
    estimated: "待确认",
    unknown: "待确认",
    unavailable: "未找到官方入口",
    closed: "暂停开放"
  };
  return labels[status] ?? status ?? "待确认";
}

function hasOfficialReservationEntry(result: TicketLookupResult) {
  return Boolean(result.sourceUrl && String(result.credibilityRank || "").toLowerCase() === "official");
}

function rankLabel(rank: string) {
  const labels: Record<string, string> = {
    official: "官方",
    aggregator: "聚合",
    search: "搜索",
    mock: "模拟",
    unknown: "未知",
    unavailable: "查询失败"
  };
  return labels[rank] ?? rank;
}

function formatTime(value: string) {
  if (!value) {
    return "待查询";
  }
  return value.replace("T", " ").slice(0, 16);
}

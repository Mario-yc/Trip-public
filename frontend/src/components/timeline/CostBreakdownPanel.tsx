import type { ItineraryPlan, RouteOption, TicketLookupResult } from "../../services/apiClient";
import { authoritativeBudgetSummary } from "./itineraryWorkspace";

type CostBreakdownPanelProps = {
  id?: string;
  labelledBy?: string;
  plan: ItineraryPlan | null;
};

type CostRow = {
  id: string;
  label: string;
  amount: number | null;
  source: string;
  status?: string;
  certainty: "confirmed" | "estimated" | "provisional" | "unknown";
};

type CostGroup = {
  key: string;
  title: string;
  rows: CostRow[];
};

export function CostBreakdownPanel({ id, labelledBy, plan }: CostBreakdownPanelProps) {
  if (!plan) {
    return (
      <section aria-label={labelledBy ? undefined : "费用明细"} aria-labelledby={labelledBy} className="cost-breakdown-panel" id={id} role="tabpanel">
        <h2>费用明细</h2>
        <p>行程生成后会展示餐饮、交通、票务、住宿和其它预算。</p>
      </section>
    );
  }
  const groups = buildCostGroups(plan);
  const localDetailTotal = groups.flatMap((group) => group.rows).reduce((sum, row) => sum + (row.amount ?? 0), 0);
  const summary = authoritativeBudgetSummary(plan, localDetailTotal);
  return (
    <section aria-label={labelledBy ? undefined : "费用明细"} aria-labelledby={labelledBy} className="cost-breakdown-panel" id={id} role="tabpanel">
      <header className="cost-breakdown-header">
        <div>
          <h2>费用明细</h2>
          <p>{budgetTierLabel(summary.tier)}；金额上限{summary.numericTarget === null ? "未指定" : `¥${Math.round(summary.numericTarget)}`}。</p>
        </div>
        <div aria-label="费用可信度汇总">
          <strong>当前可估（暂估） ¥{Math.round(summary.provisionalPreferred)}</strong>
          <span>已知合计 ¥{Math.round(summary.knownTotal)}</span>
          <span>暂估范围 ¥{Math.round(summary.provisionalMin)}–¥{Math.round(summary.provisionalMax)}</span>
          <span>未知 {summary.unknownItems.length} 项</span>
        </div>
      </header>
      {summary.unknownItems.length ? (
        <ul aria-label="预算未知项" className="cost-breakdown-unknown-items">
          {summary.unknownItems.map((item) => <li key={item}>{item}</li>)}
        </ul>
      ) : null}
      <div className="cost-breakdown-groups">
        {groups.map((group) => {
          const subtotal = group.rows.reduce((sum, row) => sum + (row.amount ?? 0), 0);
          const unknownCount = group.rows.filter((row) => row.certainty === "unknown").length;
          return (
            <section className="cost-breakdown-group" key={group.key}>
              <header>
                <h3>{group.title}</h3>
                <span>可估 ¥{Math.round(subtotal)}{unknownCount ? ` · 未知 ${unknownCount} 项` : ""}</span>
              </header>
              {group.rows.length ? (
                <ul>
                  {group.rows.map((row) => (
                    <li key={row.id}>
                      <span>{row.label}</span>
                      <strong>{row.amount === null ? "待查询" : `¥${Math.round(row.amount)}`}</strong>
                      <small>{certaintyLabel(row.certainty)} · {row.status ? `${row.status} · ` : ""}{row.source}</small>
                    </li>
                  ))}
                </ul>
              ) : (
                <p>暂无该类费用。</p>
              )}
            </section>
          );
        })}
      </div>
    </section>
  );
}

function budgetTierLabel(tier: ItineraryPlan["budgetTier"] | string) {
  return ({ low: "低预算", medium: "中等预算", high: "高预算" } as Record<string, string>)[tier ?? ""] ?? "预算档位待确认";
}

function buildCostGroups(plan: ItineraryPlan): CostGroup[] {
  const ticketBySegment = new Map((plan.ticketLookupResults ?? []).map((ticket) => [ticket.segmentId, ticket]));
  const selectedRoutes = billableRoutes(plan.routeOptions ?? []);
  const rows = plan.days.flatMap((day) =>
    day.segments.map((segment) => ({
      dayNumber: day.dayNumber,
      segment,
      ticket: ticketBySegment.get(segment.id)
    }))
  );
  return [
    {
      key: "meal",
      title: "餐饮",
      rows: rows
        .filter(({ segment }) => segment.kind === "meal")
        .map(({ dayNumber, segment }) => costRowFromSegment(
          `meal-${segment.id}`,
          `Day ${dayNumber} · ${segment.poi.name}`,
          segment,
          "budget_policy"
        ))
    },
    {
      key: "transport",
      title: "交通",
      rows: selectedRoutes.map((route) => ({
        id: `route-${route.id}`,
        label: routeLabel(plan, route),
        amount: positiveOrNull(route.costAmount),
        source: route.provider || route.source || "AMap route cost",
        certainty: positiveOrNull(route.costAmount) === null ? "unknown" as const : "estimated" as const
      }))
    },
    {
      key: "tickets",
      title: "景点/票务",
      rows: rows
        .filter(({ segment }) => segment.kind === "visit" || segment.kind === "activity")
        .map(({ dayNumber, segment, ticket }) => ticketCostRow(dayNumber, segment.id, segment.poi.name, segment.estimatedCost, ticket))
    },
    {
      key: "lodging",
      title: "住宿",
      rows: rows
        .filter(({ segment }) => segment.kind === "lodging")
        .map(({ dayNumber, segment }) => costRowFromSegment(
          `lodging-${segment.id}`,
          `Day ${dayNumber} · ${segment.poi.name}`,
          segment,
          "budget_policy",
          true
        ))
    },
    {
      key: "other",
      title: "其它",
      rows: rows
        .filter(({ segment }) => !["meal", "visit", "activity", "lodging"].includes(segment.kind))
        .map(({ dayNumber, segment }) => costRowFromSegment(
          `other-${segment.id}`,
          `Day ${dayNumber} · ${segment.poi.name}`,
          segment,
          "budget_policy"
        ))
    }
  ];
}

function ticketCostRow(dayNumber: number, segmentId: string, poiName: string, estimatedCost: number, ticket?: TicketLookupResult): CostRow {
  if (ticket) {
    const amount = positiveOrNull(ticket.priceEstimate) ?? positiveOrNull(estimatedCost);
    return {
      id: `ticket-${ticket.id}`,
      label: `Day ${dayNumber} · ${poiName}`,
      amount,
      source: ticket.sourceName || ticket.providerName || "ticket_lookup",
      status: ticket.status,
      certainty: amount === null ? "unknown" : "confirmed"
    };
  }
  return {
    id: `ticket-pending-${segmentId}`,
    label: `Day ${dayNumber} · ${poiName}`,
    amount: null,
    source: "pending",
    status: estimatedCost > 0 ? `票务待查询（旧估算 ¥${Math.round(estimatedCost)} 不计入）` : "票务待查询",
    certainty: "unknown"
  };
}

function costRowFromSegment(
  id: string,
  label: string,
  segment: ItineraryPlan["days"][number]["segments"][number],
  fallbackSource: string,
  unknownWithoutMetadata = false
): CostRow {
  const metadata = segment.estimateMetadata?.cost;
  const status = normalizeCertainty(metadata?.status, metadata?.provisional, Boolean(metadata));
  const certainty = unknownWithoutMetadata && !metadata ? "unknown" : status;
  const amount = certainty === "unknown"
    ? null
    : positiveOrNull(metadata?.preferred) ?? positiveOrNull(segment.estimatedCost);
  return {
    id,
    label,
    amount,
    source: metadata?.source || fallbackSource,
    certainty: amount === null ? "unknown" : certainty
  };
}

function normalizeCertainty(
  status: string | undefined,
  provisional: boolean | undefined,
  hasMetadata: boolean
): CostRow["certainty"] {
  if (["confirmed", "estimated", "provisional", "unknown"].includes(status ?? "")) {
    return status as CostRow["certainty"];
  }
  if (provisional === true) {
    return "provisional";
  }
  return hasMetadata ? "estimated" : "estimated";
}

function certaintyLabel(certainty: CostRow["certainty"]) {
  return {
    confirmed: "已确认",
    estimated: "估算",
    provisional: "暂估",
    unknown: "未知"
  }[certainty];
}

function routeLabel(plan: ItineraryPlan, route: RouteOption) {
  const names = plan.days
    .flatMap((day) => day.segments)
    .filter((segment) => segment.id === route.fromSegmentId || segment.id === route.toSegmentId)
    .map((segment) => segment.poi.name);
  return names.length >= 2 ? `${route.label} · ${names[0]} -> ${names[1]}` : route.label;
}

function billableRoutes(routes: RouteOption[]) {
  const normalRoutes = routes.filter((route) => route.isSelected && !isDrivingTaxi(route) && route.costAmount > 0);
  const drivingTaxiByLeg = new Map<string, RouteOption>();
  for (const route of routes) {
    if (!route.isSelected || !isDrivingTaxi(route) || route.costAmount <= 0) {
      continue;
    }
    const key = `${route.fromSegmentId ?? ""}->${route.toSegmentId ?? ""}:${route.fromPoiId}->${route.toPoiId}`;
    const current = drivingTaxiByLeg.get(key);
    if (!current || route.costAmount > current.costAmount) {
      drivingTaxiByLeg.set(key, { ...route, label: "驾车/打车" });
    }
  }
  return [...normalRoutes, ...drivingTaxiByLeg.values()];
}

function isDrivingTaxi(route: RouteOption) {
  return ["driving", "taxi", "self_drive"].includes(route.mode || route.transportMode);
}

function positiveOrNull(value: number | null | undefined) {
  return typeof value === "number" && value > 0 ? value : null;
}

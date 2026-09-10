import { PlanComparisonResponse } from "../../services/apiClient";
import {
  comparisonPlanReadiness,
  isCurrentComparisonScope,
  type ComparisonPlanProjection,
  type PlanComparisonPreviewState
} from "../../state/planComparisonPreview";
import { TicketSourceList } from "./TicketSourceList";

type PlanComparisonProps = {
  comparison: PlanComparisonResponse | null;
  errorMessage?: string;
  preview?: PlanComparisonPreviewState;
  focusedDayNumber?: number | null;
  onFocusPlan?: (proposalId: string) => void;
  onFocusPlanDay?: (proposalId: string, dayNumber: number) => void;
  onOpenPlanDetails?: (proposalId: string) => void;
  onAdoptPlan?: (plan: ComparisonPlanProjection) => void;
  onCompleteTheme?: (plan: ComparisonPlanProjection) => void;
  onRepairPlan?: (plan: ComparisonPlanProjection, planIndex: number) => void;
  adoptingChoiceId?: string | null;
  completingChoiceId?: string | null;
};

export function PlanComparison({
  comparison,
  errorMessage = "",
  preview,
  focusedDayNumber,
  onFocusPlan,
  onFocusPlanDay,
  onOpenPlanDetails,
  onAdoptPlan,
  onCompleteTheme,
  onRepairPlan,
  adoptingChoiceId,
  completingChoiceId
}: PlanComparisonProps) {
  if (preview && (preview.plans.length > 0 || preview.comparisonSummary)) {
    const indexedPlans = preview.plans.map((plan, planIndex) => ({ plan, planIndex }));
    const readyPlans = indexedPlans.filter(({ plan }) => isConfirmationReadyPlan(plan));
    const partialPlans = indexedPlans.filter(({ plan }) => !isConfirmationReadyPlan(plan));
    const summary = comparisonSummaryViewModel(preview, readyPlans.length, partialPlans.length);
    return (
      <section className="comparison-panel" aria-label="Plan comparison">
        <h2>行程对比</h2>
        <p>主对比仅展示已经通过采用与路线门禁的方案；有真实缺口的方向保留在待补全区。</p>
        <p aria-live="polite" className="comparison-summary-strip" role="status">
          <span>方案池可确认 {summary.adoptionReadyCount} 个</span>
          <span>方案池待补全 {summary.repairablePartialCount} 个</span>
          {summary.hasAuthoritativeFrontier ? (
            <span>仍有 {summary.remainingQualifiedEntityCount} 个未探索高校候选</span>
          ) : null}
        </p>
        {readyPlans.length > 0 ? (
          <div className="comparison-grid">
            {readyPlans.map(({ plan, planIndex }) => (
              <ComparisonPlanCard
                adoptingChoiceId={adoptingChoiceId}
                completingChoiceId={completingChoiceId}
                focusedDayNumber={focusedDayNumber}
                key={`${plan.planningSelectionRootTurnId}:${plan.rootPortfolioId}:${plan.proposalId}`}
                onAdoptPlan={onAdoptPlan}
                onCompleteTheme={onCompleteTheme}
                onFocusPlan={onFocusPlan}
                onFocusPlanDay={onFocusPlanDay}
                onOpenPlanDetails={onOpenPlanDetails}
                onRepairPlan={onRepairPlan}
                plan={plan}
                planIndex={planIndex}
                preview={preview}
                sectionKind="ready"
              />
            ))}
          </div>
        ) : (
          <p className="comparison-empty-ready">当前还没有路线证据完整、可确认编辑的方案。</p>
        )}
        {partialPlans.length > 0 ? (
          <details className="comparison-partial-section">
            <summary>待补全方向（{summary.repairablePartialCount}）</summary>
            <p>这些方向只读保留真实候选和缺口，不计入可确认方案。</p>
            <div className="comparison-grid">
              {partialPlans.map(({ plan, planIndex }) => (
                <ComparisonPlanCard
                  adoptingChoiceId={adoptingChoiceId}
                  completingChoiceId={completingChoiceId}
                  focusedDayNumber={focusedDayNumber}
                  key={`${plan.planningSelectionRootTurnId}:${plan.rootPortfolioId}:${plan.proposalId}`}
                  onAdoptPlan={onAdoptPlan}
                  onCompleteTheme={onCompleteTheme}
                  onFocusPlan={onFocusPlan}
                  onFocusPlanDay={onFocusPlanDay}
                  onOpenPlanDetails={onOpenPlanDetails}
                  onRepairPlan={onRepairPlan}
                  plan={plan}
                  planIndex={planIndex}
                  preview={preview}
                  sectionKind="partial"
                />
              ))}
            </div>
          </details>
        ) : null}
        {summary.frontierMessage ? (
          <p className={`comparison-frontier-status ${summary.frontierStatus ?? "unknown"}`}>
            {summary.frontierMessage}
          </p>
        ) : null}
        {summary.constraintModificationLabel ? (
          <p className="comparison-constraint-entry">
            可在对话区选择“{summary.constraintModificationLabel}”；执行时仍以服务端签发的操作身份为准。
          </p>
        ) : null}
      </section>
    );
  }
  if (!comparison) {
    return (
      <section className="comparison-panel" aria-label="Plan comparison">
        <h2>多个方案比较</h2>
        {errorMessage ? <p role="alert">{errorMessage}</p> : <p>生成行程后展示低预算、拍照优先、轻松不赶路方案。</p>}
      </section>
    );
  }

  return (
    <section className="comparison-panel" aria-label="Plan comparison">
      <h2>多个方案比较</h2>
      {comparison.fallbackUsed ? <p>{comparison.userVisibleCaveat}</p> : null}
      <div className="comparison-grid">
        {comparison.plans.map((plan) => (
          <article className="comparison-card" key={plan.id}>
            <h3>{templateLabel(plan.templateType)}</h3>
            <p>{plan.decisionRationale}</p>
            <p>预算估算：{plan.budgetEstimate} CNY</p>
            <p>{plan.budgetDeltaExplanation}</p>
            <TicketSourceList results={plan.ticketLookupResults.slice(0, 3)} />
          </article>
        ))}
      </div>
    </section>
  );
}

type ComparisonPlanCardProps = {
  adoptingChoiceId?: string | null;
  completingChoiceId?: string | null;
  focusedDayNumber?: number | null;
  onAdoptPlan?: (plan: ComparisonPlanProjection) => void;
  onCompleteTheme?: (plan: ComparisonPlanProjection) => void;
  onFocusPlan?: (proposalId: string) => void;
  onFocusPlanDay?: (proposalId: string, dayNumber: number) => void;
  onOpenPlanDetails?: (proposalId: string) => void;
  onRepairPlan?: (plan: ComparisonPlanProjection, planIndex: number) => void;
  plan: ComparisonPlanProjection;
  planIndex: number;
  preview: PlanComparisonPreviewState;
  sectionKind: "ready" | "partial";
};

function ComparisonPlanCard({
  adoptingChoiceId,
  completingChoiceId,
  focusedDayNumber,
  onAdoptPlan,
  onCompleteTheme,
  onFocusPlan,
  onFocusPlanDay,
  onOpenPlanDetails,
  onRepairPlan,
  plan,
  planIndex,
  preview,
  sectionKind
}: ComparisonPlanCardProps) {
  const isCurrent = isCurrentComparisonScope(preview, plan);
  const hasOpeningConflict = Boolean(plan.verifiedScheduleConflicts?.length);
  const displayedDays = comparisonDaysForDisplay(plan);
  const allowLegacyNonSimpleAction = sectionKind === "partial" && !isSimpleDirection(plan);
  const canRepair = Boolean(
    sectionKind === "partial" && isCurrent && isSimpleDirection(plan) && plan.repairChoiceId?.trim() && onRepairPlan
  );
  return (
    <article
      aria-label={`方案 ${planIndex + 1}：${plan.title}`}
      aria-selected={preview.focusedProposalId === plan.proposalId}
      className={`comparison-card comparison-color-${plan.colorKey}${preview.focusedProposalId === plan.proposalId ? " focused" : ""}${isCurrent ? "" : " historical"}`}
      data-adoption-ready={String(plan.adoptionReady)}
      data-choice-id={plan.choiceId}
      data-comparison-scope={isCurrent ? "current" : "historical"}
      data-confirmation-passed={String(comparisonPlanReadiness(plan).confirmationReady)}
      data-is-partial={String(sectionKind === "partial")}
      data-proposal-id={plan.proposalId}
      data-section={sectionKind}
      onClick={() => onFocusPlan?.(plan.proposalId)}
      onDoubleClick={(event) => {
        if (event.target !== event.currentTarget && (event.target as HTMLElement).closest("button")) return;
        onFocusPlan?.(plan.proposalId);
        onOpenPlanDetails?.(plan.proposalId);
      }}
      onKeyDown={(event) => {
        if (event.target !== event.currentTarget) return;
        if (event.key === "Enter") {
          event.preventDefault();
          onFocusPlan?.(plan.proposalId);
          onOpenPlanDetails?.(plan.proposalId);
        } else if (event.key === " ") {
          event.preventDefault();
          onFocusPlan?.(plan.proposalId);
        }
      }}
      role="button"
      tabIndex={0}
    >
      <div className="comparison-card-heading">
        <span className="comparison-color-swatch" aria-hidden="true" />
        <h3>{plan.title}</h3>
        <button
          className="comparison-view-details"
          onClick={(event) => {
            event.stopPropagation();
            onFocusPlan?.(plan.proposalId);
            onOpenPlanDetails?.(plan.proposalId);
          }}
          onDoubleClick={(event) => event.stopPropagation()}
          onKeyDown={(event) => event.stopPropagation()}
          type="button"
        >
          查看详情
        </button>
      </div>
      {plan.guideEvidenceUsage?.status === "satisfied" && plan.guideEvidenceUsage.usedPlaces.length > 0 ? (
        <div className="comparison-guide-evidence" aria-label="攻略证据采用情况">
          <p>参考攻略：已采用 {uniqueGuidePlaceNames(plan.guideEvidenceUsage.usedPlaces).join("、")}</p>
          <p>攻略来源可追溯 · 高德身份已核验 · 路线已核验</p>
        </div>
      ) : null}
      <p>
        {isCurrent
          ? plan.comparisonRole === "current_active_draft"
            ? "当前草稿"
            : sectionKind === "partial"
              ? `当前方向：${completionLabel(plan)}`
              : completionLabel(plan)
          : "历史方案，仅供对比"}
      </p>
      {displayedDays.map(({ day, dayNumber }) => {
        const segments = day?.segments ?? [];
        const dayStatus = comparisonDayStatusLabel(plan, dayNumber, segments.length);
        return (
          <div
            aria-label={`查看方案 ${planIndex + 1} Day ${dayNumber} 地图`}
            aria-pressed={preview.focusedProposalId === plan.proposalId && focusedDayNumber === dayNumber}
            className={`comparison-day${
              preview.focusedProposalId === plan.proposalId && focusedDayNumber === dayNumber ? " focused" : ""
            }`}
            key={`${plan.proposalId}:${day?.id ?? `day_${dayNumber}`}`}
            onClick={(event) => {
              event.stopPropagation();
              onFocusPlanDay?.(plan.proposalId, dayNumber);
            }}
            onDoubleClick={(event) => event.stopPropagation()}
            onKeyDown={(event) => {
              if (event.target !== event.currentTarget || !["Enter", " "].includes(event.key)) return;
              event.preventDefault();
              event.stopPropagation();
              onFocusPlanDay?.(plan.proposalId, dayNumber);
            }}
            role="button"
            tabIndex={0}
          >
            <strong>Day {dayNumber}</strong>
            <div className="comparison-route-sequence">
              {segments.map((segment) => segment.poi.name).join(" → ") || dayStatus || "暂无已验证地点"}
            </div>
            {dayStatus && segments.length > 0 ? <div className="comparison-day-status">{dayStatus}</div> : null}
            {segments.map((segment) => {
              const visitFact = plan.visitFactsBySegment?.[segment.id];
              const openingDetail = visitFact ? openingFactDetailLabel(visitFact) : undefined;
              return (
                <div
                  className="comparison-segment-time"
                  key={`${plan.proposalId}:${day?.id ?? `day_${dayNumber}`}:${segment.id}`}
                  title={openingDetail}
                >
                  ○ {comparisonSegmentTimeLabel(segment, visitFact)} · {segment.poi.name}
                  {segment.semanticMetadata?.isAutoSupplemented === true ? (
                    <span className="comparison-supplement-badge">
                      {` · 顺路补充${supplementEvidenceLabel(segment.semanticMetadata)}`}
                    </span>
                  ) : null}
                  {mealEvidenceLabel(segment) ? (
                    <span className="comparison-meal-evidence"> · {mealEvidenceLabel(segment)}</span>
                  ) : null}
                </div>
              );
            })}
            {plan.pendingSlots
              .filter((slot) => slot.dayNumber === dayNumber)
              .map((slot) => (
                <div
                  className="comparison-pending-slot"
                  key={`${slot.briefId ?? ""}:${slot.poolId ?? ""}:${slot.planningSlotId}:${slot.dayNumber}`}
                >
                  ○ {slot.timeLabel || slot.timeWindow || "时间待定"} · {slot.displayNeed}
                </div>
              ))}
          </div>
        );
      })}
      <div aria-label="方案就绪状态" className="comparison-readiness-status">
        {plan.openingFactsRefreshStatus === "refreshing" || plan.openingFactsRefreshStatus === "not_started" ? (
          <p>正在核验开放时间…</p>
        ) : null}
        {hasOpeningConflict ? (
          <p className="comparison-direct-block">该时间与已核验开放时间冲突，请补全或重新生成该方向。</p>
        ) : null}
        <p>{hardStatusLabel(plan)}</p>
        <p>{softStatusLabel(plan)}</p>
        <p>{routeProgressLabel(plan)}</p>
        <p>{detourComplianceLabel(plan)}</p>
        {routeComfortLabel(plan) ? <p>{routeComfortLabel(plan)}</p> : null}
        {directAdoptionBlockLabel(plan) ? (
          <p className="comparison-direct-block">采用条件：{directAdoptionBlockLabel(plan)}</p>
        ) : null}
      </div>
      <p>{budgetSummaryLabel(plan)}</p>
      <p>{plan.tradeoffSummary}</p>
      {(sectionKind === "ready" || allowLegacyNonSimpleAction) && plan.completionAction && isCurrent ? (
        <button
          className="comparison-theme-completion-action"
          disabled={completingChoiceId === plan.completionAction.choiceId}
          onClick={(event) => {
            event.stopPropagation();
            onCompleteTheme?.(plan);
          }}
          type="button"
        >
          {completingChoiceId === plan.completionAction.choiceId ? "正在补全主题…" : plan.completionAction.label}
        </button>
      ) : null}
      {plan.blockingReasons?.length ? (
        <p className="comparison-blocking-reasons">
          待处理：
          {(plan.blockingReasonLabels?.length
            ? plan.blockingReasonLabels
            : plan.blockingReasons.map(blockingReasonLabel)
          ).join("；")}
        </p>
      ) : null}
      {canRepair ? (
        <button
          className="comparison-repair-action"
          data-choice-id={plan.repairChoiceId}
          onClick={(event) => {
            event.stopPropagation();
            onFocusPlan?.(plan.proposalId);
            onRepairPlan?.(plan, planIndex);
          }}
          type="button"
        >
          补全此方案
        </button>
      ) : null}
      {(sectionKind === "ready" || allowLegacyNonSimpleAction) && canShowPlanAction(preview, plan) ? (
        <button
          disabled={
            hasOpeningConflict || (!isSimpleDirection(plan) && plan.isAdopted) || adoptingChoiceId === plan.choiceId
          }
          onClick={(event) => {
            event.stopPropagation();
            onAdoptPlan?.(plan);
          }}
          type="button"
        >
          {hasOpeningConflict
            ? "开放时间冲突，暂不可确认"
            : !isSimpleDirection(plan) && plan.isAdopted
              ? "已采用"
              : adoptingChoiceId === plan.choiceId
                ? actionProgressLabel(plan)
                : resolvedActionLabel(plan)}
        </button>
      ) : (
        <p className="comparison-action-status" role="status">
          {isCurrent
            ? sectionKind === "partial"
              ? canRepair
                ? `可补全：${resolvedActionLabel(plan).replace(/^暂不可确认：/, "")}`
                : resolvedActionLabel(plan)
              : resolvedActionLabel(plan)
            : "新一轮规划已开始，历史方案不可再执行"}
        </p>
      )}
    </article>
  );
}

type ComparisonSummaryViewModel = {
  adoptionReadyCount: number;
  repairablePartialCount: number;
  remainingQualifiedEntityCount: number;
  hasAuthoritativeFrontier: boolean;
  frontierStatus: string | null;
  frontierMessage: string;
  constraintModificationLabel: string;
};

function comparisonSummaryViewModel(
  preview: PlanComparisonPreviewState,
  renderedReadyCount: number,
  renderedPartialCount: number
): ComparisonSummaryViewModel {
  const currentPlans = preview.plans.filter((plan) => isCurrentComparisonScope(preview, plan));
  const hasCurrentPlans = currentPlans.length > 0;
  const currentReadyCount = hasCurrentPlans ? currentPlans.filter(isConfirmationReadyPlan).length : renderedReadyCount;
  const currentPartialCount = hasCurrentPlans
    ? currentPlans.filter((plan) => !isConfirmationReadyPlan(plan)).length
    : renderedPartialCount;
  const summary = preview.comparisonSummary;
  if (!summary) {
    return {
      adoptionReadyCount: currentReadyCount,
      repairablePartialCount: currentPartialCount,
      remainingQualifiedEntityCount: 0,
      hasAuthoritativeFrontier: false,
      frontierStatus: null,
      frontierMessage: "",
      constraintModificationLabel: ""
    };
  }
  return {
    adoptionReadyCount: currentReadyCount,
    repairablePartialCount: currentPartialCount,
    remainingQualifiedEntityCount: summary.remainingQualifiedEntityCount,
    hasAuthoritativeFrontier: true,
    frontierStatus: summary.frontierStatus,
    frontierMessage: comparisonFrontierMessage(summary),
    constraintModificationLabel:
      summary.constraintModificationChoiceId && summary.constraintModificationLabel
        ? summary.constraintModificationLabel
        : ""
  };
}

function comparisonFrontierMessage(summary: NonNullable<PlanComparisonPreviewState["comparisonSummary"]>): string {
  const remainingUniversityCount = summary.remainingQualifiedEntityCount;
  const remainingPoiPageCount = summary.remainingPoiPageCount ?? 0;
  const remainingFrontier =
    remainingUniversityCount > 0
      ? `仍有 ${remainingUniversityCount} 所合格高校尚未探索`
      : remainingPoiPageCount > 0
        ? `当前高校对仍有 ${remainingPoiPageCount} 页独立体验候选尚未检查`
        : "候选前沿仍有服务端可证明的未检查项";
  if (summary.frontierStatus === "has_more" && summary.lastOutcomeReason === "candidate_collision_frontier_remaining") {
    return `本轮组合未通过差异要求，已跳过；${remainingFrontier}。`;
  }
  if (summary.frontierStatus === "has_more") {
    return `候选前沿仍可继续：${remainingFrontier}。`;
  }
  if (summary.frontierStatus === "provider_pending") {
    return "当前地点或路线核验尚未完成，候选前沿没有被误标为耗尽；请使用服务端提供的恢复或补全操作。";
  }
  const hasQualifiedEntityEvidence = (summary.exploredQualifiedEntityCount ?? 0) > 0;
  const hasPoiPageEvidence = (summary.attemptedPoiPageCount ?? 0) > 0;
  const checked = hasQualifiedEntityEvidence
    ? `已检查 ${summary.exploredQualifiedEntityCount} 个资格实体`
    : hasPoiPageEvidence
      ? `已检查 ${summary.attemptedPoiPageCount} 页地点候选`
      : "候选前沿已检查";
  const pages = hasQualifiedEntityEvidence && hasPoiPageEvidence ? `、${summary.attemptedPoiPageCount} 页地点候选` : "";
  const layer = summary.blockingLayer ? `；阻断层级：${summary.blockingLayer}` : "";
  const reasons: Record<string, string> = {
    qualification_exhausted: "没有尚未探索且证据合格的高校实体",
    poi_exhausted: "地点候选页已检查完，仍无法补齐独立体验",
    route_feasible_exhausted: "候选组合均未通过紧凑性或真实路线门禁"
  };
  return `${reasons[summary.frontierStatus] ?? "当前候选前沿已停止"}（${checked}${pages}${layer}）。`;
}

function isConfirmationReadyPlan(plan: ComparisonPlanProjection): boolean {
  return comparisonPlanReadiness(plan).confirmationReady;
}

function comparisonDaysForDisplay(plan: ComparisonPlanProjection) {
  const daysByNumber = new Map(plan.days.map((day) => [day.dayNumber, day]));
  const dayNumbers = new Set([
    ...plan.days.map((day) => day.dayNumber),
    ...(plan.requiredPlanningDayNumbers ?? []),
    ...(plan.explicitRestDayNumbers ?? []),
    ...(plan.uncoveredDayNumbers ?? [])
  ]);
  return [...dayNumbers]
    .sort((left, right) => left - right)
    .map((dayNumber) => ({ dayNumber, day: daysByNumber.get(dayNumber) }));
}

function comparisonDayStatusLabel(plan: ComparisonPlanProjection, dayNumber: number, segmentCount: number): string {
  if (plan.explicitRestDayNumbers?.includes(dayNumber) && segmentCount === 0) return "休息/自由活动日";
  if (plan.uncoveredDayNumbers?.includes(dayNumber)) return `Day ${dayNumber} 尚未完成有效地点规划`;
  if (plan.requiredPlanningDayNumbers?.includes(dayNumber) && segmentCount === 0) {
    return `Day ${dayNumber} 尚未完成有效地点规划`;
  }
  if (
    segmentCount === 0 &&
    (plan.requiredPlanningDayNumbers !== undefined || plan.explicitRestDayNumbers !== undefined)
  ) {
    return `Day ${dayNumber} 尚未完成有效地点规划`;
  }
  return "";
}

function supplementEvidenceLabel(metadata: Record<string, unknown>): string {
  const adjacent = Array.isArray(metadata.adjacentAnchorNames)
    ? metadata.adjacentAnchorNames.map((item) => String(item || "").trim()).filter(Boolean)
    : [];
  const addedTravelMinutes = Number(metadata.addedTravelMinutes);
  const routeEvidence = Number.isFinite(addedTravelMinutes)
    ? `，新增交通约 ${Math.max(0, Math.round(addedTravelMinutes))} 分钟`
    : "";
  return adjacent.length === 2 ? `（位于 ${adjacent[0]} → ${adjacent[1]} 之间${routeEvidence}）` : routeEvidence;
}

function templateLabel(templateType: string) {
  const labels: Record<string, string> = {
    low_budget: "低预算",
    photo_first: "拍照优先",
    relaxed_pace: "轻松不赶路",
    custom: "自定义"
  };
  return labels[templateType] ?? templateType;
}

type ComparisonSegment = ComparisonPlanProjection["days"][number]["segments"][number];
type ComparisonVisitFact = NonNullable<ComparisonPlanProjection["visitFactsBySegment"]>[string];

function comparisonSegmentTimeLabel(segment: ComparisonSegment, visitFact?: ComparisonVisitFact): string {
  const metadata = segment.semanticMetadata;
  const decision = metadata?.scheduleDecision;
  const exact =
    segment.startTime && segment.endTime ? `${segment.startTime}-${segment.endTime}` : segment.startTime || "时间待排";
  const reasons = Array.isArray(decision?.provisionalReasons)
    ? decision.provisionalReasons.map((reason) => String(reason))
    : [];
  const isProvisional = decision?.scheduleConfidence === "provisional" && exact !== "时间待排";
  const openingPending =
    decision?.openingEvidenceStatus === "unverified" || reasons.some((reason) => /open|开放/i.test(reason));
  const routePending =
    decision?.routeArrivalEvidenceStatus === "unverified" ||
    reasons.some((reason) => /route|arrival|travel|路线|到达/i.test(reason));
  const openingStatus = visitFact
    ? openingFactScheduleLabel(visitFact)
    : isProvisional && openingPending
      ? "开放时间待核验"
      : "";
  const suffix = [openingStatus, isProvisional && routePending ? "到达时间待核验" : ""].filter(Boolean).join("；");
  const scheduled = isProvisional ? `预计 ${exact}` : exact;
  if (suffix) return `${scheduled}（${suffix}）`;
  return isProvisional ? `${scheduled}（排期依据待核验）` : scheduled;
}

function openingFactScheduleLabel(fact: ComparisonVisitFact): string {
  const opening = fact.openingHours ?? fact.facts?.openingHours;
  if (fact.refreshStatus === "expired") return "开放信息已过期";
  if (!opening || opening.status === "unknown" || opening.status === "failed") {
    return `${shortVisitDate(fact.visitDate)}开放时间未核实`;
  }
  if (opening.status === "conflicting") return "开放信息有冲突";
  if (opening.status !== "verified") return "开放时间提示待核实";
  if (opening.effectiveForDate !== fact.visitDate) return "开放信息已查到，到访日期待核实";
  const window = compactOpeningWindow(opening);
  if (fact.scheduleCompatibility === "verified_conflict") {
    return window ? `开放 ${window}，时间冲突` : "开放时间冲突";
  }
  if (fact.scheduleCompatibility === "verified_compatible") {
    return window ? `开放 ${window}，已核验` : "开放时间已核验";
  }
  return window ? `开放 ${window}，时段待确认` : "当日开放信息已核验，时段待确认";
}

function openingFactDetailLabel(fact: ComparisonVisitFact): string {
  const summary = openingFactScheduleLabel(fact);
  const opening = fact.openingHours ?? fact.facts?.openingHours;
  const raw = String(opening?.valueText ?? "").trim();
  return raw && raw !== "待核验" && !summary.includes(raw) ? `${summary}；来源摘要：${raw}` : summary;
}

function compactOpeningWindow(opening: NonNullable<ComparisonVisitFact["openingHours"]>): string {
  const structuredIntervals = opening.structuredValue?.intervals;
  const intervalLabels = Array.isArray(structuredIntervals)
    ? structuredIntervals
        .map((item) => {
          if (!item || typeof item !== "object" || Array.isArray(item)) return "";
          const record = item as Record<string, unknown>;
          const start = String(record.start ?? "").trim();
          const end = String(record.end ?? "").trim();
          return /^\d{1,2}:\d{2}$/.test(start) && /^\d{1,2}:\d{2}$/.test(end) ? `${start}-${end}` : "";
        })
        .filter(Boolean)
    : [];
  if (intervalLabels.length) return [...new Set(intervalLabels)].slice(0, 2).join("/");
  const ranges = String(opening.valueText ?? "").match(
    /(?:[01]?\d|2[0-3]):[0-5]\d\s*(?:-|–|—|至)\s*(?:[01]?\d|2[0-3]):[0-5]\d/g
  );
  return [...new Set((ranges ?? []).map((value) => value.replace(/\s*(?:-|–|—|至)\s*/, "-")))]
    .slice(0, 2)
    .join("/");
}

function shortVisitDate(value: string): string {
  const match = String(value ?? "").match(/^\d{4}-(\d{2})-(\d{2})$/);
  return match ? `${Number(match[1])}月${Number(match[2])}日` : "当日";
}

function mealEvidenceLabel(segment: ComparisonSegment): string {
  const metadata = segment.semanticMetadata as Record<string, unknown> | undefined;
  const constraints = metadata?.scheduleConstraints;
  if (!constraints || typeof constraints !== "object" || Array.isArray(constraints)) return "";
  const evidence = (constraints as Record<string, unknown>).mealSemanticEvidence;
  if (!evidence || typeof evidence !== "object" || Array.isArray(evidence)) return "";
  const record = evidence as Record<string, unknown>;
  const theme = typeof record.themeLabel === "string" ? record.themeLabel.trim() : "";
  const matchedTerms = Array.isArray(record.matchedTerms)
    ? record.matchedTerms.filter((item): item is string => typeof item === "string" && item.trim().length > 0)
    : [];
  const localKind = typeof record.localFoodEvidenceKind === "string" ? record.localFoodEvidenceKind : "";
  const sourceLabel =
    localKind === "amap_destination_cuisine_subtype"
      ? "高德目的地菜系分类"
      : localKind === "supporting_local_food_claim"
        ? "可追溯当地饮食来源"
        : localKind === "amap_city_marker_and_theme"
          ? "高德地点与主题字段"
          : "";
  if (!theme && matchedTerms.length === 0) return "";
  return `${theme || matchedTerms[0]}主题；${sourceLabel || "高德字段"}命中“${matchedTerms[0] || theme}”`;
}

function routeComfortLabel(plan: ComparisonPlanProjection): string {
  const evidence = plan.routeComfortEvidence;
  if (!evidence) return "";
  const seconds = typeof evidence.totalTravelSeconds === "number" ? evidence.totalTravelSeconds : null;
  const maxSeconds = typeof evidence.maxAdjacentTravelSeconds === "number" ? evidence.maxAdjacentTravelSeconds : null;
  if (seconds === null || maxSeconds === null) return "";
  return `路线舒适度证据：总交通约 ${Math.ceil(seconds / 60)} 分钟，最长相邻移动约 ${Math.ceil(maxSeconds / 60)} 分钟`;
}

function completionLabel(plan: ComparisonPlanProjection): string {
  const readiness = comparisonPlanReadiness(plan);
  const hardPending = plan.pendingHardSlotCount ?? 0;
  const softPending = plan.pendingSoftSlotCount ?? Math.max(0, plan.pendingSlots.length - hardPending);
  if (isSimpleDirection(plan) && !readiness.dayContractPresent) return "每日行程完整性待核验";
  if (isSimpleDirection(plan) && readiness.uncoveredDayNumbers.length > 0) {
    return `仍有 ${readiness.uncoveredDayNumbers.length} 个行程日待补全`;
  }
  if (hardPending > 0) return `仍缺 ${hardPending} 个必选地点`;
  if (plan.adoptionMode === "editable_draft" || plan.adoptionMode === "editable_partial") {
    return `仍有 ${softPending} 个体验位置待选择`;
  }
  if (softPending > 0) return `仍有 ${softPending} 个体验位置待选择`;
  if (plan.routePreconditionFailureReason) {
    return "地点已补齐 · 路线前置条件未满足";
  }
  if (plan.routeStatus === "route_provider_failed") return "地点已补齐 · 路线服务暂不可用";
  if (!["route_ready", "route_not_required"].includes(plan.routeStatus ?? "route_pending")) {
    return "地点已补齐 · 路线待核验";
  }
  if (plan.blockingReasons?.some((reason) => reason.includes("map_identity"))) return "部分地点待地图确认";
  return readiness.confirmationReady ? "完整方案" : "地点与路线已核验 · 采用条件未满足";
}

function hardStatusLabel(plan: ComparisonPlanProjection): string {
  const count = plan.pendingHardSlotCount ?? 0;
  return count > 0 ? `必选地点：仍缺 ${count} 个` : "必选地点：已完成";
}

function detourComplianceLabel(plan: ComparisonPlanProjection): string {
  if (plan.detourCompliance === "verified") return "绕行约束：真实路线已验证";
  if (plan.detourCompliance === "exceeded") return "绕行约束：已超出，方案不可采用";
  return "绕行约束：路线待核验";
}

function softStatusLabel(plan: ComparisonPlanProjection): string {
  const hardPending = plan.pendingHardSlotCount ?? 0;
  const count = plan.pendingSoftSlotCount ?? Math.max(0, plan.pendingSlots.length - hardPending);
  return count > 0 ? `待选体验：${count} 个待补` : "待选体验：已完成";
}

function directAdoptionBlockLabel(plan: ComparisonPlanProjection): string {
  const readiness = comparisonPlanReadiness(plan);
  if (readiness.confirmationReady) return "";
  if (isSimpleDirection(plan) && !readiness.dayContractPresent) return "每日行程完整性合同待核验";
  if (isSimpleDirection(plan) && readiness.uncoveredDayNumbers.length > 0) {
    return `Day ${readiness.uncoveredDayNumbers.join("、Day ")} 尚未完成有效地点规划`;
  }
  const hardPending = plan.pendingHardSlotCount ?? 0;
  if (hardPending > 0) return `仍缺 ${hardPending} 个必选地点`;
  const expected = plan.routeExpectedLegCount ?? 0;
  const verified = plan.routeVerifiedLegCount ?? 0;
  if (expected > verified) return `路线还差 ${expected - verified} 段，补齐后才能采用`;
  if (plan.routePreconditionFailureReason) return "路线前置条件未满足，暂不可采用";
  if (plan.blockingReasons?.some((reason) => reason.includes("map_identity"))) return "部分地点待地图确认";
  const softPending = plan.pendingSoftSlotCount ?? plan.pendingSlots.length;
  if (softPending > 0) return `仍有 ${softPending} 个待选体验需要补全`;
  if (isSimpleDirection(plan) && plan.confirmationPassed !== true) return "方案尚未通过最终完整性核验";
  if (isSimpleDirection(plan) && !readiness.routeCoverageComplete) return "相邻路线核验尚未完整";
  return "未满足采用条件";
}

function adoptionBlockLabel(plan: ComparisonPlanProjection): string {
  if (plan.comparisonRole === "current_active_draft") return "继续编辑";
  return directAdoptionBlockLabel(plan) || "未满足采用条件";
}

function resolvedActionLabel(plan: ComparisonPlanProjection): string {
  const hardPending = plan.pendingHardSlotCount ?? 0;
  const softPending = plan.pendingSoftSlotCount ?? Math.max(0, plan.pendingSlots.length - hardPending);
  if (isSimpleDirection(plan)) {
    if (canConfirmSimpleDirection(plan)) {
      const serverLabel = plan.nextActionLabel?.trim();
      return serverLabel && serverLabel !== "确认编辑" ? serverLabel : `确认编辑「${plan.title || "该行程方向"}」`;
    }
    const blockers = plan.blockingReasonLabels?.length
      ? plan.blockingReasonLabels
      : (plan.blockingReasons ?? []).map(blockingReasonLabel);
    return blockers.length ? `暂不可确认：${blockers.join("；")}` : "暂不可确认：方案采用条件待核验";
  }
  if (plan.nextAction === "complete_pending_slots") {
    return `补全 ${softPending || plan.pendingSlots.length} 个待选体验`;
  }
  if (plan.nextAction === "adopt_editable_draft" || (plan.nextAction === "adopt_proposal" && softPending > 0)) {
    return `采用为可编辑草案（仍可补充 ${softPending} 项）`;
  }
  if (plan.nextAction === "continue_grounding_hard_slots" || (plan.nextAction === "none" && hardPending > 0)) {
    return plan.nextActionLabel || `补齐 ${hardPending} 个必选地点`;
  }
  return plan.nextActionLabel || adoptionBlockLabel(plan);
}

function routeProgressLabel(plan: ComparisonPlanProjection): string {
  const expected = plan.routeExpectedLegCount ?? 0;
  const verified = plan.routeVerifiedLegCount ?? 0;
  const base = plan.routeSummary || "路线待核验";
  if (expected <= 0) return base;
  const retryable = plan.routeRetryable ? " · 可重试" : "";
  if (/\d+\s*\/\s*\d+\s*段/.test(base)) return `${base}${retryable}`;
  return `${base}（${verified}/${expected} 段）${retryable}`;
}

function canRunNextAction(plan: ComparisonPlanProjection): boolean {
  return [
    "complete_pending_slots",
    "verify_routes",
    "verify_routes_and_adopt",
    "retry_route_verification",
    "adopt",
    "adopt_proposal",
    "adopt_editable_draft",
    "confirm_edit",
    "continue_grounding_hard_slots",
    "continue_editing"
  ].includes(plan.nextAction ?? "none");
}

function actionProgressLabel(plan: ComparisonPlanProjection): string {
  if (isSimpleDirection(plan)) return "正在进入编辑…";
  if (plan.nextAction === "complete_pending_slots") return "正在补全…";
  if (["verify_routes", "verify_routes_and_adopt", "retry_route_verification"].includes(plan.nextAction ?? "")) {
    return "正在核验路线…";
  }
  return "正在采用…";
}

function isSimpleDirection(plan: ComparisonPlanProjection): boolean {
  return plan.workflowMode === "simple_direction_v1";
}

function canConfirmSimpleDirection(plan: ComparisonPlanProjection): boolean {
  return comparisonPlanReadiness(plan).confirmationReady;
}

function canShowPlanAction(preview: PlanComparisonPreviewState, plan: ComparisonPlanProjection): boolean {
  if (isSimpleDirection(plan)) return isCurrentComparisonScope(preview, plan) && canConfirmSimpleDirection(plan);
  return isCurrentComparisonScope(preview, plan) && (canRunNextAction(plan) || plan.isAdopted);
}

function blockingReasonLabel(reason: string): string {
  const labels: Record<string, string> = {
    route_evidence_incomplete: "路线证据未完整",
    pending_slots_remaining: "仍有待补时段",
    map_identity_incomplete: "部分地点待地图确认",
    semantic_coverage_failed: "地点语义校验未通过",
    proposal_verifier_not_passed: "方案严格校验未通过",
    spatial_focus_grounding_required: "活动区域仍待确认",
    campus_tier_985_mismatch: "校园资格证据不足",
    local_food_evidence_missing: "当地餐饮与当前城市的关联证据不足",
    topology_constraint_exceeded: "日内顺序回折超过当前偏好",
    adjacent_leg_limit_exceeded: "相邻公交行程超过当前时长上限",
    provider_route_matrix_incomplete: "高德相邻路线证据未完整"
  };
  const density = reason.match(/^route_anchor_target_mismatch:day_(\d+):(\d+)\/(\d+)$/);
  if (density) return `第 ${density[1]} 天计划 ${density[3]} 个地点，已确认 ${density[2]} 个`;
  const required = reason.match(/^required_goal_count_insufficient:([^:]+):(\d+)\/(\d+)$/);
  if (required)
    return required[1] === "goal_night_view"
      ? `夜景必选地点需要 ${required[3]} 个，当前确认 ${required[2]} 个`
      : `必选地点需要 ${required[3]} 个，当前确认 ${required[2]} 个`;
  if (reason === "required_goal_omitted:goal_night_view") return "夜景必选目标尚未加入";
  if (reason === "theme_optional_family_missing") return "该方案的主题体验尚未补齐";
  if (reason === "portfolio_route_quality:route_evidence_missing") return "仍缺与当前停靠顺序一致的路线核验";
  return labels[reason] ?? "方案条件待核验";
}

function budgetSummaryLabel(plan: ComparisonPlanProjection): string {
  const tierLabels: Record<string, string> = {
    low: "低预算",
    medium: "中等预算",
    high: "较高预算",
    unknown: "预算档位待确认"
  };
  const raw = plan.budgetSummary || "预算档位待确认 · 预算待核验";
  const [rawTier, ...statusParts] = raw
    .split("·")
    .map((item) => item.trim())
    .filter(Boolean);
  const tier = plan.budgetTierLabel || tierLabels[plan.budgetTier ?? rawTier.toLowerCase()] || rawTier;
  const status = statusParts.join(" · ") || "预算待核验";
  return `${tier || "预算档位待确认"} · ${status}`;
}

function uniqueGuidePlaceNames(
  places: NonNullable<ComparisonPlanProjection["guideEvidenceUsage"]>["usedPlaces"]
): string[] {
  return [...new Set(places.map((place) => place.mentionText.trim()).filter(Boolean))];
}

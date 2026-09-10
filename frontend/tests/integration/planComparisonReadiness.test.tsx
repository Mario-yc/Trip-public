import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";

import { PlanComparison } from "../../src/components/comparison/PlanComparison";
import type { ComparisonPlanProjection, PlanComparisonPreviewState } from "../../src/state/planComparisonPreview";

afterEach(cleanup);

function preview(overrides: Record<string, unknown> = {}): PlanComparisonPreviewState {
  return {
    planningSelectionRootTurnId: "turn_root",
    rootPortfolioId: "portfolio_root",
    focusedProposalId: "proposal_1",
    adoptedProposalId: null,
    adoptedVersionId: null,
    autoNavigationCompleted: true,
    autoNavigationCount: 1,
    mapMode: "plan_overview_preview",
    isMapReadOnly: true,
    plans: [
      {
        planningSelectionRootTurnId: "turn_root",
        rootPortfolioId: "portfolio_root",
        proposalId: "proposal_1",
        sourceAssistantTurnId: "turn_assistant",
        choiceId: "choice_1",
        status: "partial",
        isPartial: true,
        isAdopted: false,
        adoptionReady: false,
        activeVersionId: null,
        expectedBaseVersionId: null,
        title: "路线待核验方案",
        days: [],
        pendingSlots: [],
        routeEvidence: [],
        routeStatus: "route_pending",
        routeExpectedLegCount: 2,
        routeVerifiedLegCount: 0,
        routeRetryable: true,
        budgetSummary: "medium · 预算待核验",
        budgetStatus: "pending",
        budgetEvidenceCount: 0,
        unknownCostSegmentCount: 2,
        routeSummary: "路线待核验",
        blockingReasons: ["route_coverage_incomplete"],
        comparisonRole: "candidate_proposal",
        originProjectionMode: "partial_preview",
        currentReadiness: "route_pending",
        promotionStatus: "promotable",
        nextAction: "verify_routes_and_adopt",
        nextActionLabel: "核验路线并采用",
        tradeoffSummary: "",
        colorKey: "ocean",
        ...overrides
      }
    ]
  };
}

function verifiedSegment(dayNumber: number, segmentNumber: number) {
  const suffix = `${dayNumber}_${segmentNumber}`;
  return {
    id: `seg_${suffix}`,
    startTime: `${8 + segmentNumber * 3}:00`,
    endTime: `${9 + segmentNumber * 3}:00`,
    kind: segmentNumber === 2 ? "meal" : "visit",
    poi: {
      id: `poi_${suffix}`,
      amapId: `B000${dayNumber}${segmentNumber}ABCDE`,
      name: `Day ${dayNumber} 地点 ${segmentNumber}`,
      city: "北京",
      category: segmentNumber === 2 ? "food" : "scenic",
      source: "amap-place-search",
      latitude: 39.9 + dayNumber / 100 + segmentNumber / 1000,
      longitude: 116.3 + dayNumber / 100 + segmentNumber / 1000,
      confidence: 0.98
    },
    transportMode: "transit",
    estimatedCost: 0,
    notes: ""
  };
}

function twoCompletedDays() {
  return [1, 2].map((dayNumber) => ({
    id: `day_${dayNumber}`,
    dayNumber,
    title: `Day ${dayNumber}`,
    weatherSummary: "",
    riskSummary: "",
    totalEstimatedCost: 0,
    segments: [1, 2, 3].map((segmentNumber) => verifiedSegment(dayNumber, segmentNumber))
  }));
}

function completeMealProjectionEvidence(dayNumbers: number[] = [1, 2]) {
  return {
    mealQualityPassed: true,
    mealDiversityPassed: true,
    mealUnresolvedReasons: [] as string[],
    mealThemeSignature: dayNumbers.map((dayNumber) => `theme-${dayNumber}`),
    mealSemanticEvidence: dayNumbers.map((dayNumber) => ({
      dayNumber,
      planningSlotId: `meal-day-${dayNumber}`,
      amapPoiId: `B000${dayNumber}2ABCDE`,
      canonicalBrand: `brand-${dayNumber}`,
      groundedFamilyKey: `theme-${dayNumber}`,
      themeGrounded: true,
      localFoodPassed: true
    }))
  };
}

describe("PlanComparison readiness copy", () => {
  test("shows traceable guide adoption only for a satisfied guide evidence usage", () => {
    const base = preview();
    render(
      <PlanComparison
        comparison={null}
        preview={{
          ...preview(),
          plans: [
            {
              ...base.plans[0],
              guideEvidenceUsage: {
                schemaVersion: "guide-evidence-usage-v1",
                status: "satisfied",
                evidenceFingerprint: "evidence_1",
                requiredMinimum: 1,
                usedPlaces: [
                  {
                    mentionText: "北海公园",
                    intentType: "park",
                    sourceRefIds: ["guide-ref-1"],
                    amapPoiId: "B0001",
                    physicalIdentityKey: "amap:B0001",
                    dayNumber: 1,
                    planningSlotId: "slot_park_1",
                    routeVerified: true
                  }
                ],
                rejectionCounts: {}
              }
            }
          ]
        }}
      />
    );

    expect(screen.getByText("参考攻略：已采用 北海公园")).toBeTruthy();
    expect(screen.getByText("攻略来源可追溯 · 高德身份已核验 · 路线已核验")).toBeTruthy();
  });

  test("does not present an unsatisfied or empty guide evidence usage as adoption", () => {
    const base = preview();
    render(
      <PlanComparison
        comparison={null}
        preview={{
          ...preview(),
          plans: [
            {
              ...base.plans[0],
              guideEvidenceUsage: {
                schemaVersion: "guide-evidence-usage-v1",
                status: "unsatisfied",
                evidenceFingerprint: "evidence_1",
                requiredMinimum: 1,
                usedPlaces: [],
                rejectionCounts: { no_amap_match: 1 }
              }
            }
          ]
        }}
      />
    );

    expect(screen.queryByText(/参考攻略：已采用/)).toBeNull();
  });

  test("shows date-scoped opening facts and blocks a verified schedule conflict", () => {
    const days = twoCompletedDays();
    const segment = days[0].segments[0];
    render(
      <PlanComparison
        comparison={null}
        preview={preview({
          workflowMode: "simple_direction_v1",
          status: "complete",
          isPartial: false,
          adoptionReady: true,
          confirmationPassed: true,
          requiredPlanningDayNumbers: [1, 2],
          explicitRestDayNumbers: [],
          uncoveredDayNumbers: [],
          days,
          ...completeMealProjectionEvidence(),
          routeStatus: "route_ready",
          routeExpectedLegCount: 4,
          routeVerifiedLegCount: 4,
          routeErrorLegCount: 0,
          blockingReasons: [],
          nextAction: "confirm_edit",
          nextActionLabel: "确认编辑",
          openingFactsRefreshStatus: "completed",
          visitFactsBySegment: {
            [segment.id]: {
              segmentId: segment.id,
              amapPoiId: segment.poi.amapId,
              visitDate: "2026-10-01",
              refreshStatus: "partial",
              facts: {},
              openingHours: {
                status: "verified",
                valueText: "09:00-10:00",
                effectiveForDate: "2026-10-01",
                sourceRefs: [],
                queriedAt: "2026-09-01T12:00:00Z",
                expiresAt: "2026-09-02T12:00:00Z"
              },
              sourceRefs: [],
              evidenceFingerprint: "evidence",
              queriedAt: "2026-09-01T12:00:00Z",
              expiresAt: "2026-09-02T12:00:00Z",
              scheduleCompatibility: "verified_conflict"
            }
          },
          verifiedScheduleConflicts: [{ segmentId: segment.id }]
        })}
      />
    );

    expect(screen.getByText(/（开放 09:00-10:00，时间冲突）/)).toBeTruthy();
    expect(screen.queryByText(/开放时间已核验：09:00-10:00/)).toBeNull();
    expect((screen.getByRole("button", { name: "开放时间冲突，暂不可确认" }) as HTMLButtonElement).disabled).toBe(true);
  });

  test("keeps an unsuccessful date-scoped opening lookup inside the schedule parentheses", () => {
    const days = twoCompletedDays();
    const segment = days[0].segments[0];
    render(
      <PlanComparison
        comparison={null}
        preview={preview({
          days,
          openingFactsRefreshStatus: "partial",
          visitFactsBySegment: {
            [segment.id]: {
              segmentId: segment.id,
              amapPoiId: segment.poi.amapId,
              visitDate: "2026-10-01",
              refreshStatus: "failed",
              facts: {},
              openingHours: {
                status: "unknown",
                valueText: "待核验",
                effectiveForDate: null,
                sourceRefs: [],
                queriedAt: "2026-09-01T12:00:00Z",
                expiresAt: "2026-09-02T12:00:00Z"
              },
              sourceRefs: [],
              evidenceFingerprint: "unknown-evidence",
              queriedAt: "2026-09-01T12:00:00Z",
              expiresAt: "2026-09-02T12:00:00Z",
              scheduleCompatibility: "unknown"
            }
          }
        })}
      />
    );

    expect(screen.getByText(/（10月1日开放时间未核实）/)).toBeTruthy();
    expect(screen.queryByText(/未找到适用于 2026-10-01 的可靠开放时间/)).toBeNull();
  });

  test("shows exact-date hours compactly without overstating a generic official summary", () => {
    const days = twoCompletedDays();
    const verifiedSegment = days[0].segments[0];
    const genericSegment = days[0].segments[1];
    render(
      <PlanComparison
        comparison={null}
        preview={preview({
          days,
          openingFactsRefreshStatus: "partial",
          visitFactsBySegment: {
            [verifiedSegment.id]: {
              segmentId: verifiedSegment.id,
              amapPoiId: verifiedSegment.poi.amapId,
              visitDate: "2026-10-01",
              refreshStatus: "partial",
              facts: {},
              openingHours: {
                status: "verified",
                valueText: "周一至周日 09:00-17:00",
                structuredValue: { intervals: [{ start: "09:00", end: "17:00" }] },
                effectiveForDate: "2026-10-01",
                sourceRefs: [],
                queriedAt: "2026-09-01T12:00:00Z",
                expiresAt: "2026-09-02T12:00:00Z"
              },
              sourceRefs: [],
              evidenceFingerprint: "verified-evidence",
              queriedAt: "2026-09-01T12:00:00Z",
              expiresAt: "2026-09-02T12:00:00Z",
              scheduleCompatibility: "verified_compatible"
            },
            [genericSegment.id]: {
              segmentId: genericSegment.id,
              amapPoiId: genericSegment.poi.amapId,
              visitDate: "2026-10-01",
              refreshStatus: "partial",
              facts: {},
              openingHours: {
                status: "verified",
                valueText: "非全时开放；节假日安排请关注官方通知。",
                effectiveForDate: null,
                sourceRefs: [],
                queriedAt: "2026-09-01T12:00:00Z",
                expiresAt: "2026-09-02T12:00:00Z"
              },
              sourceRefs: [],
              evidenceFingerprint: "generic-evidence",
              queriedAt: "2026-09-01T12:00:00Z",
              expiresAt: "2026-09-02T12:00:00Z",
              scheduleCompatibility: "unknown"
            }
          }
        })}
      />
    );

    expect(screen.getByText(/（开放 09:00-17:00，已核验）/)).toBeTruthy();
    expect(screen.getByText(/（开放信息已查到，到访日期待核实）/)).toBeTruthy();
    expect(screen.queryByText(/非全时开放；节假日安排请关注官方通知/)).toBeNull();
  });
  test("separates ready and partial directions and renders the authoritative frontier summary", () => {
    const onRepairPlan = vi.fn();
    const base = preview();
    const partialPlan = {
      ...base.plans[0],
      workflowMode: "simple_direction_v1" as const,
      confirmationPassed: false,
      pendingHardSlotCount: 1,
      repairChoiceId: "repair_partial_opaque",
      title: "待补公园方向"
    };
    const readyPlan = {
      ...base.plans[0],
      proposalId: "proposal_ready",
      choiceId: "confirm_ready_opaque",
      status: "complete",
      isPartial: false,
      adoptionReady: true,
      confirmationPassed: true,
      workflowMode: "simple_direction_v1" as const,
      requiredPlanningDayNumbers: [1, 2],
      explicitRestDayNumbers: [],
      uncoveredDayNumbers: [],
      days: twoCompletedDays(),
      ...completeMealProjectionEvidence(),
      routeStatus: "route_ready",
      routeExpectedLegCount: 4,
      routeVerifiedLegCount: 4,
      blockingReasons: [],
      nextAction: "confirm_edit" as const,
      nextActionLabel: "确认编辑",
      title: "工大河畔寻踪"
    };
    render(
      <PlanComparison
        comparison={null}
        preview={{
          ...base,
          focusedProposalId: readyPlan.proposalId,
          plans: [readyPlan, partialPlan],
          comparisonSummary: {
            adoptionReadyCount: 1,
            repairablePartialCount: 1,
            remainingQualifiedEntityCount: 4,
            frontierStatus: "has_more",
            lastOutcomeReason: "candidate_collision_frontier_remaining"
          }
        }}
        onRepairPlan={onRepairPlan}
      />
    );

    expect(screen.getByText("方案池可确认 1 个")).toBeTruthy();
    expect(screen.getByText("方案池待补全 1 个")).toBeTruthy();
    expect(screen.getByText("仍有 4 个未探索高校候选")).toBeTruthy();
    expect(screen.getByText("本轮组合未通过差异要求，已跳过；仍有 4 所合格高校尚未探索。")).toBeTruthy();
    const partialSection = screen.getByText("待补全方向（1）").closest("details");
    expect(partialSection?.hasAttribute("open")).toBe(false);
    expect(screen.getByLabelText("方案 1：工大河畔寻踪").getAttribute("data-section")).toBe("ready");

    fireEvent.click(screen.getByText("待补全方向（1）"));
    const partialCard = screen.getByLabelText("方案 2：待补公园方向");
    expect(partialCard.getAttribute("data-section")).toBe("partial");
    expect(partialCard.textContent).toContain("当前方向：每日行程完整性待核验");
    expect(partialCard.textContent).toContain("必选地点：仍缺 1 个");
    expect(partialCard.querySelector('[data-choice-id="repair_partial_opaque"]')).toBeTruthy();
    expect(screen.queryByRole("button", { name: /确认编辑.*待补公园方向/ })).toBeNull();
  });

  test("does not invent a remaining-university count when the server summary is absent", () => {
    render(<PlanComparison comparison={null} preview={preview()} />);
    expect(screen.queryByText(/未探索高校候选/)).toBeNull();
  });

  test("keeps continuation truthful when only POI pages remain after a novelty collision", () => {
    render(
      <PlanComparison
        comparison={null}
        preview={{
          ...preview(),
          comparisonSummary: {
            adoptionReadyCount: 0,
            repairablePartialCount: 1,
            remainingQualifiedEntityCount: 0,
            remainingPoiPageCount: 3,
            frontierStatus: "has_more",
            lastOutcomeReason: "candidate_collision_frontier_remaining"
          }
        }}
      />
    );

    expect(screen.getByText("本轮组合未通过差异要求，已跳过；当前高校对仍有 3 页独立体验候选尚未检查。")).toBeTruthy();
  });

  test("does not claim qualification entities for a generic paged POI frontier", () => {
    render(
      <PlanComparison
        comparison={null}
        preview={{
          ...preview(),
          comparisonSummary: {
            adoptionReadyCount: 0,
            repairablePartialCount: 1,
            remainingQualifiedEntityCount: 0,
            remainingPoiPageCount: 0,
            attemptedPoiPageCount: 2,
            exploredQualifiedEntityCount: 0,
            frontierStatus: "poi_exhausted",
            blockingLayer: "poi",
            lastOutcomeReason: "no_material_novelty"
          }
        }}
      />
    );

    expect(
      screen.getByText("地点候选页已检查完，仍无法补齐独立体验（已检查 2 页地点候选；阻断层级：poi）。")
    ).toBeTruthy();
    expect(screen.queryByText(/资格实体/)).toBeNull();
  });

  test("does not label unverified route alternatives as exhausted", () => {
    render(
      <PlanComparison
        comparison={null}
        preview={{
          ...preview(),
          comparisonSummary: {
            adoptionReadyCount: 0,
            repairablePartialCount: 1,
            remainingQualifiedEntityCount: 0,
            remainingPoiPageCount: 0,
            frontierStatus: "provider_pending",
            blockingLayer: "provider"
          }
        }}
      />
    );

    expect(
      screen.getByText("当前地点或路线核验尚未完成，候选前沿没有被误标为耗尽；请使用服务端提供的恢复或补全操作。")
    ).toBeTruthy();
  });

  test("offers a proposal-scoped repair action without pretending the blocked card is adoptable", () => {
    const onRepairPlan = vi.fn();
    const onFocusPlan = vi.fn();
    render(
      <PlanComparison
        comparison={null}
        preview={preview({
          workflowMode: "simple_direction_v1",
          materialFingerprint: "material_fp",
          repairChoiceId: "repair_choice",
          pendingHardSlotCount: 1,
          pendingSlots: [
            {
              planningSlotId: "day2_campus",
              dayNumber: 2,
              displayNeed: "高校地点",
              requirementLevel: "required"
            }
          ],
          blockingReasons: ["simple_direction_planned_day_missing_verified_amap_anchor"],
          nextAction: "none",
          nextActionLabel: "第 2 天高校待补"
        })}
        onFocusPlan={onFocusPlan}
        onRepairPlan={onRepairPlan}
      />
    );

    fireEvent.click(screen.getByRole("button", { name: "补全此方案" }));
    expect(onFocusPlan).toHaveBeenCalledWith("proposal_1");
    expect(onRepairPlan).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("button", { name: /^确认编辑/ })).toBeNull();
  });

  test("labels a dynamically scheduled unverified opening time as provisional", () => {
    render(
      <PlanComparison
        comparison={null}
        preview={preview({
          days: [
            {
              id: "day_1",
              dayNumber: 1,
              title: "Day 1",
              weatherSummary: "",
              riskSummary: "",
              totalEstimatedCost: 0,
              segments: [
                {
                  id: "seg_park",
                  startTime: "18:25",
                  endTime: "19:45",
                  kind: "park",
                  poi: {
                    id: "B000PARK",
                    amapId: "B000PARK",
                    name: "城市公园",
                    city: "北京",
                    category: "scenic",
                    source: "amap-place-search",
                    latitude: 39.9,
                    longitude: 116.4
                  },
                  transportMode: "walk",
                  estimatedCost: 0,
                  notes: "",
                  semanticMetadata: {
                    scheduleDecision: {
                      scheduleConfidence: "provisional",
                      openingEvidenceStatus: "unverified"
                    }
                  }
                }
              ]
            }
          ]
        })}
      />
    );

    expect(screen.getByText(/预计 18:25-19:45（开放时间待核验）/)).toBeTruthy();
  });

  test("renders flexible segments in sealed sequence without inventing a clock", () => {
    render(
      <PlanComparison
        comparison={null}
        preview={preview({
          days: [
            {
              id: "day_1",
              dayNumber: 1,
              title: "Day 1",
              weatherSummary: "",
              riskSummary: "",
              totalEstimatedCost: 0,
              segments: [
                {
                  id: "seg_campus",
                  startTime: "",
                  endTime: "",
                  kind: "visit",
                  poi: {
                    id: "B000CAMPUS",
                    amapId: "B000CAMPUS",
                    name: "测试高校",
                    city: "北京",
                    category: "education",
                    source: "amap-place-search",
                    latitude: 39.9,
                    longitude: 116.4
                  },
                  transportMode: "transit",
                  estimatedCost: 0,
                  notes: "",
                  semanticMetadata: {
                    schedulePreference: { sequence: 1 },
                    scheduleDecision: { scheduleConfidence: "unresolved" }
                  }
                },
                {
                  id: "seg_meal",
                  startTime: "12:00",
                  endTime: "13:00",
                  kind: "meal",
                  poi: {
                    id: "B000MEAL",
                    amapId: "B000MEAL",
                    name: "测试餐厅",
                    city: "北京",
                    category: "food",
                    source: "amap-place-search",
                    latitude: 39.91,
                    longitude: 116.41
                  },
                  transportMode: "walk",
                  estimatedCost: 0,
                  notes: "",
                  semanticMetadata: {
                    schedulePreference: { sequence: 2 },
                    scheduleDecision: { scheduleConfidence: "verified_constraints" }
                  }
                }
              ]
            }
          ]
        })}
      />
    );

    const dayText = screen.getByText("测试高校 → 测试餐厅");
    expect(dayText).toBeTruthy();
    expect(screen.getByText(/时间待排 · 测试高校/)).toBeTruthy();
    expect(screen.getByText(/12:00-13:00 · 测试餐厅/)).toBeTruthy();
  });

  test("shows a route-gap supplement only with its factual adjacent-route evidence", () => {
    render(
      <PlanComparison
        comparison={null}
        preview={preview({
          days: [
            {
              id: "day_1",
              dayNumber: 1,
              title: "Day 1",
              weatherSummary: "",
              riskSummary: "",
              totalEstimatedCost: 0,
              segments: [
                {
                  id: "seg_gap",
                  startTime: "11:00",
                  endTime: "12:00",
                  kind: "visit",
                  poi: {
                    id: "B000GAPPARK",
                    amapId: "B000GAPPARK",
                    name: "沿途口袋公园",
                    city: "北京",
                    category: "scenic",
                    source: "amap-place-search",
                    latitude: 39.95,
                    longitude: 116.35
                  },
                  transportMode: "transit",
                  estimatedCost: 0,
                  notes: "",
                  semanticMetadata: {
                    isAutoSupplemented: true,
                    adjacentAnchorNames: ["清华大学", "颐和园公园"],
                    addedTravelMinutes: 8,
                    scheduleDecision: { scheduleConfidence: "verified_constraints" }
                  }
                }
              ]
            }
          ]
        })}
      />
    );

    expect(screen.getByText(/顺路补充（位于 清华大学 → 颐和园公园 之间，新增交通约 8 分钟）/)).toBeTruthy();
  });

  test("offers route verification instead of permanently disabling a complete-location candidate", () => {
    const onAdoptPlan = vi.fn();
    render(<PlanComparison comparison={null} preview={preview()} onAdoptPlan={onAdoptPlan} />);

    expect(screen.getByText("当前方向：地点已补齐 · 路线待核验")).toBeTruthy();
    expect(screen.getByText("路线待核验（0/2 段） · 可重试")).toBeTruthy();
    expect(screen.getByText("中等预算 · 预算待核验")).toBeTruthy();
    const action = screen.getByRole<HTMLButtonElement>("button", { name: "核验路线并采用" });
    expect(action.disabled).toBe(false);
    fireEvent.click(action);
    expect(onAdoptPlan).toHaveBeenCalledTimes(1);
    expect(screen.queryByText(/仍有 0 个时段待补/)).toBeNull();
    expect(screen.queryByText("完整方案")).toBeNull();
    expect(screen.getByText("绕行约束：路线待核验")).toBeTruthy();
  });

  test("shows verified detour compliance only when backend route assignment is verified", () => {
    render(<PlanComparison comparison={null} preview={preview({ detourCompliance: "verified" })} />);
    expect(screen.getByText("绕行约束：真实路线已验证")).toBeTruthy();
  });

  test("shows a route precondition failure truthfully without retry or provider-outage copy", () => {
    render(
      <PlanComparison
        comparison={null}
        preview={preview({
          routeStatus: "route_provider_failed",
          routeProviderAttemptCount: 0,
          routePreconditionFailureReason: "route_provider_not_invoked",
          routeRetryable: false,
          routeSummary: "路线前置条件未满足",
          nextAction: "none",
          nextActionLabel: "路线前置条件未满足"
        })}
      />
    );

    expect(screen.getByText("当前方向：地点已补齐 · 路线前置条件未满足")).toBeTruthy();
    expect(screen.getByText("路线前置条件未满足（0/2 段）")).toBeTruthy();
    expect(screen.queryByText(/路线服务暂不可用/)).toBeNull();
    expect(screen.queryByText(/可重试/)).toBeNull();
    expect(screen.getByText("路线前置条件未满足")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "路线前置条件未满足" })).toBeNull();
  });

  test("renders a hard-slot blocker as status instead of an unsigned disabled operation", () => {
    render(
      <PlanComparison
        comparison={null}
        preview={preview({
          pendingHardSlotCount: 1,
          nextAction: "none",
          nextActionLabel: "仍缺 1 个必选地点",
          blockingReasons: ["required_goal_missing"]
        })}
      />
    );

    expect(screen.getAllByText("仍缺 1 个必选地点").length).toBeGreaterThan(0);
    expect(screen.queryByRole("button", { name: "仍缺 1 个必选地点" })).toBeNull();
  });

  test("renders structured Chinese blockers without exposing internal reason codes", () => {
    render(
      <PlanComparison
        comparison={null}
        preview={preview({
          blockingReasons: [
            "route_anchor_target_mismatch:day_1:2/4",
            "required_goal_count_insufficient:goal_night_view:0/2",
            "portfolio_route_quality:route_evidence_missing"
          ],
          blockingReasonLabels: [
            "第 1 天计划 4 个地点，已确认 2 个",
            "夜景必选地点需要 2 个，当前确认 0 个",
            "仍缺与当前停靠顺序一致的路线核验"
          ]
        })}
      />
    );

    expect(screen.getByText(/第 1 天计划 4 个地点，已确认 2 个/)).toBeTruthy();
    expect(screen.getByText(/夜景必选地点需要 2 个，当前确认 0 个/)).toBeTruthy();
    expect(screen.queryByText(/route_anchor_target_mismatch/)).toBeNull();
    expect(screen.queryByText(/required_goal_count_insufficient/)).toBeNull();
  });

  test("shows the exact adjacent-route deficit and detour blocker without an adoption action", () => {
    render(
      <PlanComparison
        comparison={null}
        preview={preview({
          workflowMode: "simple_direction_v1",
          confirmationPassed: false,
          routeStatus: "route_pending",
          routeExpectedLegCount: 2,
          routeVerifiedLegCount: 0,
          routeRetryable: false,
          routeSummary: "相邻路线证据不足 0/2 段",
          blockingReasons: ["topology_constraint_exceeded"],
          blockingReasonLabels: ["日内停靠顺序回折超过当前偏好"],
          nextAction: "none",
          nextActionLabel: "暂不可确认"
        })}
      />
    );

    expect(screen.getByText("相邻路线证据不足 0/2 段")).toBeTruthy();
    expect(screen.getAllByText(/日内停靠顺序回折超过当前偏好/).length).toBeGreaterThan(0);
    expect(screen.queryByText(/0\/2 段（0\/2 段）/)).toBeNull();
    expect(screen.queryByRole("button", { name: /确认编辑|采用此方案/ })).toBeNull();
  });

  test("claims complete only when readiness and route facts are ready", () => {
    render(
      <PlanComparison
        comparison={null}
        preview={preview({
          status: "complete",
          isPartial: false,
          adoptionReady: true,
          routeStatus: "route_ready",
          routeExpectedLegCount: 2,
          routeVerifiedLegCount: 2,
          blockingReasons: [],
          nextAction: "adopt",
          nextActionLabel: "采用此方案"
        })}
      />
    );

    expect(screen.getByText("完整方案")).toBeTruthy();
    expect(screen.getByRole<HTMLButtonElement>("button", { name: "采用此方案" }).disabled).toBe(false);
  });

  test("labels the current active draft and offers route completion rather than adoption", () => {
    const rendered = render(
      <PlanComparison
        comparison={null}
        preview={preview({
          comparisonRole: "current_active_draft",
          activeVersionId: "ver_active",
          nextAction: "verify_routes",
          nextActionLabel: "补全路线"
        })}
        onAdoptPlan={vi.fn()}
      />
    );

    expect(screen.getByText("当前草稿")).toBeTruthy();
    expect(screen.getByRole<HTMLButtonElement>("button", { name: "补全路线" }).disabled).toBe(false);
    expect(rendered.getByRole("button", { name: "补全路线" }).textContent).not.toBe("采用此方案");
  });

  test("keeps the pending-slot next action enabled instead of treating every false readiness as disabled", () => {
    const onAdoptPlan = vi.fn();
    render(
      <PlanComparison
        comparison={null}
        preview={preview({
          pendingSoftSlotCount: 3,
          pendingSlots: [
            { planningSlotId: "slot_1", dayNumber: 1, displayNeed: "market_walk" },
            { planningSlotId: "slot_2", dayNumber: 1, displayNeed: "local_food" },
            { planningSlotId: "slot_3", dayNumber: 2, displayNeed: "local_food" }
          ],
          nextAction: "complete_pending_slots",
          nextActionLabel: "补全 3 个待选体验",
          routeStatus: "route_partial",
          routeExpectedLegCount: 3,
          routeVerifiedLegCount: 2,
          routeRetryable: true
        })}
        onAdoptPlan={onAdoptPlan}
      />
    );

    const action = screen.getByRole<HTMLButtonElement>("button", { name: "补全 3 个待选体验" });
    expect(screen.getByText("必选地点：已完成")).toBeTruthy();
    expect(screen.getByText("待选体验：3 个待补")).toBeTruthy();
    expect(screen.getByText(/路线还差 1 段，补齐后才能采用/)).toBeTruthy();
    expect(action.disabled).toBe(false);
    fireEvent.click(action);
    expect(onAdoptPlan).toHaveBeenCalledTimes(1);
  });

  test("labels a soft-pending proposal as an editable draft, never as complete", () => {
    const onAdoptPlan = vi.fn();
    render(
      <PlanComparison
        comparison={null}
        preview={preview({
          adoptionReady: true,
          draftAdoptionReady: true,
          strictlyVerified: false,
          structureReady: true,
          adoptionMode: "editable_partial",
          pendingHardSlotCount: 0,
          pendingSoftSlotCount: 1,
          pendingSlots: [{ planningSlotId: "slot_local", dayNumber: 1, displayNeed: "本地生活体验" }],
          routeStatus: "route_ready",
          routeExpectedLegCount: 1,
          routeVerifiedLegCount: 1,
          blockingReasons: [],
          nextAction: "adopt_editable_draft",
          nextActionLabel: "采用草案（仍可补充 1 项）"
        })}
        onAdoptPlan={onAdoptPlan}
      />
    );

    expect(screen.getByText("仍有 1 个体验位置待选择")).toBeTruthy();
    expect(screen.queryByText("完整方案")).toBeNull();
    const action = screen.getByRole<HTMLButtonElement>("button", { name: "采用为可编辑草案（仍可补充 1 项）" });
    expect(action.disabled).toBe(false);
    fireEvent.click(action);
    expect(onAdoptPlan).toHaveBeenCalledTimes(1);
  });

  test("offers theme completion as an independent opaque action after skeleton adoption", () => {
    const onCompleteTheme = vi.fn();
    render(
      <PlanComparison
        comparison={null}
        preview={preview({
          isAdopted: true,
          activeVersionId: "ver_active",
          adoptionMode: "editable_partial",
          themeEligible: false,
          completionAction: {
            kind: "portfolio_theme_completion",
            theme: "local_food_and_area_walk",
            label: "尝试补全地方饮食与街区",
            choiceId: "theme_completion_choice_1"
          },
          nextAction: "continue_editing",
          nextActionLabel: "继续编辑"
        })}
        onCompleteTheme={onCompleteTheme}
      />
    );

    const action = screen.getByRole<HTMLButtonElement>("button", { name: "尝试补全地方饮食与街区" });
    expect(action.disabled).toBe(false);
    fireEvent.click(action);
    expect(onCompleteTheme).toHaveBeenCalledTimes(1);
  });

  test("shows route-aware segment times and labels unresolved windows as provisional", () => {
    render(
      <PlanComparison
        comparison={null}
        preview={preview({
          days: [
            {
              id: "day_1",
              dayNumber: 1,
              title: "Day 1",
              segments: [
                {
                  id: "seg_campus",
                  title: "清华大学",
                  kind: "visit",
                  startTime: "10:17",
                  endTime: "11:47",
                  poi: {
                    id: "B000A8UIN8",
                    amapId: "B000A8UIN8",
                    name: "清华大学",
                    source: "amap-place-search",
                    latitude: 40,
                    longitude: 116.3
                  }
                }
              ]
            }
          ],
          pendingSlots: [
            {
              planningSlotId: "slot_market",
              dayNumber: 1,
              displayNeed: "市井市场与传统市集",
              timeWindow: "14:00-18:00",
              timeLabel: "可安排时段 14:00-18:00（选点和交通方式确认后自动重排）",
              timingStatus: "awaiting_route_confirmation"
            }
          ]
        })}
      />
    );

    expect(screen.getByText("○ 10:17-11:47 · 清华大学")).toBeTruthy();
    expect(
      screen.getByText("○ 可安排时段 14:00-18:00（选点和交通方式确认后自动重排） · 市井市场与传统市集")
    ).toBeTruthy();
    expect(screen.queryByText("○ 14:00-18:00 · 市井市场与传统市集")).toBeNull();
  });

  test.each([
    { label: "missing", confirmationPassed: undefined },
    { label: "false", confirmationPassed: false }
  ])(
    "keeps an otherwise ready Simple Open proposal read-only when confirmationPassed is $label",
    ({ confirmationPassed }) => {
      const onAdoptPlan = vi.fn();
      const overrides: Record<string, unknown> = {
        workflowMode: "simple_direction_v1",
        status: "complete",
        isPartial: false,
        adoptionReady: true,
        requiredPlanningDayNumbers: [1, 2],
        explicitRestDayNumbers: [],
        uncoveredDayNumbers: [],
        days: twoCompletedDays(),
        ...completeMealProjectionEvidence(),
        routeStatus: "route_ready",
        routeExpectedLegCount: 4,
        routeVerifiedLegCount: 4,
        routeErrorLegCount: 0,
        blockingReasons: [],
        nextAction: "confirm_edit",
        nextActionLabel: "确认编辑"
      };
      if (confirmationPassed !== undefined) overrides.confirmationPassed = confirmationPassed;

      render(<PlanComparison comparison={null} preview={preview(overrides)} onAdoptPlan={onAdoptPlan} />);

      expect(screen.getByText("待补全方向（1）")).toBeTruthy();
      expect(screen.queryByText("完整方案")).toBeNull();
      expect(screen.queryByRole("button", { name: /确认编辑/ })).toBeNull();
      expect(onAdoptPlan).not.toHaveBeenCalled();
    }
  );

  test("renders a missing required day as incomplete instead of hiding it", () => {
    const days = twoCompletedDays().slice(0, 1);

    render(
      <PlanComparison
        comparison={null}
        preview={preview({
          workflowMode: "simple_direction_v1",
          status: "complete",
          isPartial: false,
          adoptionReady: true,
          confirmationPassed: true,
          requiredPlanningDayNumbers: [1, 2],
          explicitRestDayNumbers: [],
          uncoveredDayNumbers: [2],
          days,
          routeStatus: "route_ready",
          routeExpectedLegCount: 2,
          routeVerifiedLegCount: 2,
          routeErrorLegCount: 0,
          blockingReasons: [],
          nextAction: "confirm_edit",
          nextActionLabel: "确认编辑"
        })}
      />
    );

    expect(screen.getByText("Day 2 尚未完成有效地点规划")).toBeTruthy();
    expect(screen.getByText("待补全方向（1）")).toBeTruthy();
    expect(screen.queryByText("完整方案")).toBeNull();
    expect(screen.queryByRole("button", { name: /确认编辑/ })).toBeNull();
  });

  test("renders only an explicit rest day as rest or free activity", () => {
    const days = twoCompletedDays();
    days[1] = { ...days[1], segments: [] };

    render(
      <PlanComparison
        comparison={null}
        preview={preview({
          workflowMode: "simple_direction_v1",
          status: "complete",
          isPartial: false,
          adoptionReady: true,
          confirmationPassed: true,
          requiredPlanningDayNumbers: [1],
          explicitRestDayNumbers: [2],
          uncoveredDayNumbers: [],
          days,
          ...completeMealProjectionEvidence([1]),
          routeStatus: "route_ready",
          routeExpectedLegCount: 2,
          routeVerifiedLegCount: 2,
          routeErrorLegCount: 0,
          blockingReasons: [],
          nextAction: "confirm_edit",
          nextActionLabel: "确认编辑"
        })}
      />
    );

    expect(screen.getByText("休息/自由活动日")).toBeTruthy();
    expect(screen.queryByText("Day 2 尚未完成有效地点规划")).toBeNull();
  });

  test("requires four verified adjacent route pairs for two completed three-stop days", () => {
    const base = {
      workflowMode: "simple_direction_v1",
      status: "complete",
      isPartial: false,
      adoptionReady: true,
      confirmationPassed: true,
      requiredPlanningDayNumbers: [1, 2],
      explicitRestDayNumbers: [],
      uncoveredDayNumbers: [],
      days: twoCompletedDays(),
      ...completeMealProjectionEvidence(),
      routeStatus: "route_ready",
      routeErrorLegCount: 0,
      blockingReasons: [],
      nextAction: "confirm_edit",
      nextActionLabel: "确认编辑"
    };
    const view = render(
      <PlanComparison
        comparison={null}
        preview={preview({ ...base, routeExpectedLegCount: 2, routeVerifiedLegCount: 2 })}
      />
    );

    expect(screen.getByText("待补全方向（1）")).toBeTruthy();
    expect(screen.queryByText("完整方案")).toBeNull();
    expect(screen.queryByRole("button", { name: /确认编辑/ })).toBeNull();

    view.rerender(
      <PlanComparison
        comparison={null}
        preview={preview({ ...base, routeExpectedLegCount: 4, routeVerifiedLegCount: 4 })}
      />
    );

    expect(screen.getByText("完整方案")).toBeTruthy();
    expect(screen.getByRole("button", { name: /确认编辑/ })).toBeTruthy();
  });

  test("keeps a legacy Simple Open meal proposal read-only when meal evidence is missing", () => {
    render(
      <PlanComparison
        comparison={null}
        preview={preview({
          workflowMode: "simple_direction_v1",
          status: "complete",
          isPartial: false,
          adoptionReady: true,
          confirmationPassed: true,
          requiredPlanningDayNumbers: [1, 2],
          explicitRestDayNumbers: [],
          uncoveredDayNumbers: [],
          days: twoCompletedDays(),
          routeStatus: "route_ready",
          routeExpectedLegCount: 4,
          routeVerifiedLegCount: 4,
          routeErrorLegCount: 0,
          blockingReasons: [],
          nextAction: "confirm_edit",
          nextActionLabel: "确认编辑"
        })}
      />
    );

    expect(screen.getByText("待补全方向（1）")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /确认编辑/ })).toBeNull();
  });

  test("rejects different POIs when the verified meal family is repeated", () => {
    const repeated = completeMealProjectionEvidence();
    repeated.mealQualityPassed = false;
    repeated.mealDiversityPassed = false;
    repeated.mealThemeSignature = ["烤鸭", "烤鸭"];
    repeated.mealUnresolvedReasons = ["simple_direction_meal_family_repeated"];
    render(
      <PlanComparison
        comparison={null}
        preview={preview({
          workflowMode: "simple_direction_v1",
          status: "complete",
          isPartial: false,
          adoptionReady: true,
          confirmationPassed: true,
          requiredPlanningDayNumbers: [1, 2],
          explicitRestDayNumbers: [],
          uncoveredDayNumbers: [],
          days: twoCompletedDays(),
          ...repeated,
          routeStatus: "route_ready",
          routeExpectedLegCount: 4,
          routeVerifiedLegCount: 4,
          routeErrorLegCount: 0,
          blockingReasons: [],
          nextAction: "confirm_edit",
          nextActionLabel: "确认编辑"
        })}
      />
    );

    expect(screen.getByText("待补全方向（1）")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /确认编辑/ })).toBeNull();
  });

  test("matches backend route-pair readiness when a materialized stop requires no route edge", () => {
    const days: ComparisonPlanProjection["days"] = twoCompletedDays().slice(0, 1);
    days[0].segments[1] = {
      ...days[0].segments[1],
      semanticMetadata: { requiresRouteEdge: false }
    };

    render(
      <PlanComparison
        comparison={null}
        preview={preview({
          workflowMode: "simple_direction_v1",
          status: "complete",
          isPartial: false,
          adoptionReady: true,
          confirmationPassed: true,
          requiredPlanningDayNumbers: [1],
          explicitRestDayNumbers: [],
          uncoveredDayNumbers: [],
          days,
          ...completeMealProjectionEvidence([1]),
          routeStatus: "route_ready",
          routeExpectedLegCount: 1,
          routeVerifiedLegCount: 1,
          routeErrorLegCount: 0,
          blockingReasons: [],
          nextAction: "confirm_edit",
          nextActionLabel: "确认编辑"
        })}
      />
    );

    expect(screen.getByText("完整方案")).toBeTruthy();
    expect(screen.getByRole("button", { name: /确认编辑/ })).toBeTruthy();
  });
});

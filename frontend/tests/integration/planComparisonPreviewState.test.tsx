import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";

import { PlanComparison } from "../../src/components/comparison/PlanComparison";
import {
  beginPendingSlotComparison,
  comparisonProjectionFromUnknown,
  comparisonPreviewFromTurns,
  comparisonSummaryFromUnknown,
  createComparisonPreviewState,
  focusComparisonPlan,
  markComparisonPlanAdopted,
  perceptualPlanColorDistance,
  proposalVisitFactsNeedsAutoRefresh,
  sortPendingSlots,
  stablePlanColorKey,
  updateComparisonPlanSnapshot,
  upsertVisibleComparisonPlan
} from "../../src/state/planComparisonPreview";
import type { ComparisonPlanProjection } from "../../src/state/planComparisonPreview";

const partial: ComparisonPlanProjection = {
  planningSelectionRootTurnId: "root_1",
  rootPortfolioId: "portfolio_1",
  proposalId: "partial:ver_1",
  sourceAssistantTurnId: "assistant_1",
  choiceId: "adopt_partial_1",
  status: "partial",
  isPartial: true,
  isAdopted: false,
  adoptionReady: true,
  activeVersionId: "ver_1",
  expectedBaseVersionId: "ver_1",
  title: "文化深游（部分完成）",
  days: [],
  pendingSlots: [
    { planningSlotId: "heritage", dayNumber: 2, timeWindow: "14:00-18:00", displayNeed: "heritage_walk" },
    { planningSlotId: "unknown", dayNumber: 1, timeWindow: "", displayNeed: "时间待定" },
    { planningSlotId: "art", dayNumber: 1, timeWindow: "14:00-18:00", displayNeed: "art_walk" }
  ],
  routeEvidence: [],
  budgetSummary: "中等预算",
  routeSummary: "已验证路线 0 段",
  tradeoffSummary: "仍有 3 个时段待补",
  colorKey: stablePlanColorKey("partial:ver_1")
};

test("proposal opening facts auto-refresh runs only before the first persisted result", () => {
  expect(proposalVisitFactsNeedsAutoRefresh({ ...partial, openingFactsRefreshStatus: "not_started" })).toBe(true);
  expect(proposalVisitFactsNeedsAutoRefresh({ ...partial, openingFactsRefreshStatus: undefined })).toBe(true);
  expect(proposalVisitFactsNeedsAutoRefresh({ ...partial, openingFactsRefreshStatus: "completed" })).toBe(false);
  expect(proposalVisitFactsNeedsAutoRefresh({ ...partial, openingFactsRefreshStatus: "partial" })).toBe(false);
  expect(proposalVisitFactsNeedsAutoRefresh({ ...partial, openingFactsRefreshStatus: "failed" })).toBe(false);
  expect(
    proposalVisitFactsNeedsAutoRefresh({
      ...partial,
      openingFactsRefreshStatus: "partial",
      visitFactsBySegment: {
        expired: {
          segmentId: "expired",
          amapPoiId: "B000EXPIRED",
          visitDate: "2026-10-01",
          refreshStatus: "expired",
          facts: {},
          openingHours: {
            status: "unknown",
            valueText: "信息已过期",
            sourceRefs: [],
            queriedAt: "2026-09-01T00:00:00Z",
            expiresAt: "2026-09-02T00:00:00Z"
          },
          sourceRefs: [],
          evidenceFingerprint: "expired",
          queriedAt: "2026-09-01T00:00:00Z",
          expiresAt: "2026-09-02T00:00:00Z",
          scheduleCompatibility: "unknown"
        }
      }
    })
  ).toBe(true);
});

function simpleReadyDays(): ComparisonPlanProjection["days"] {
  return [
    {
      id: "simple_day_1",
      dayNumber: 1,
      title: "Day 1",
      weatherSummary: "待查询",
      riskSummary: "已核验",
      totalEstimatedCost: 0,
      segments: [1, 2, 3].map((segmentNumber) => ({
        id: `simple_segment_${segmentNumber}`,
        startTime: `${8 + segmentNumber * 3}:00`,
        endTime: `${9 + segmentNumber * 3}:00`,
        kind: segmentNumber === 2 ? "meal" : "activity",
        poi: {
          id: `simple_poi_${segmentNumber}`,
          amapId: `B000SIMPLE${segmentNumber}`,
          name: `已验证地点 ${segmentNumber}`,
          city: "北京",
          category: segmentNumber === 2 ? "food" : "landmark",
          latitude: 39.9 + segmentNumber / 1000,
          longitude: 116.4 + segmentNumber / 1000,
          source: "amap-place-search",
          confidence: 0.9
        },
        transportMode: "transit",
        estimatedCost: 0,
        notes: ""
      }))
    }
  ];
}

function simpleMealProjectionEvidence() {
  return {
    mealQualityPassed: true,
    mealDiversityPassed: true,
    mealUnresolvedReasons: [],
    mealThemeSignature: ["theme-1"],
    mealSemanticEvidence: [
      {
        dayNumber: 1,
        planningSlotId: "simple-meal-1",
        amapPoiId: "B000SIMPLE2",
        canonicalBrand: "simple-brand-1",
        groundedFamilyKey: "theme-1",
        themeGrounded: true,
        localFoodPassed: true
      }
    ]
  };
}

afterEach(() => cleanup());

describe("Portfolio comparison preview state", () => {
  test("same-material stale carriers cannot erase persisted opening facts", () => {
    const visitFact = {
      segmentId: "segment_visit",
      amapPoiId: "B000VISIT",
      visitDate: "2026-10-01",
      refreshStatus: "failed",
      facts: {},
      openingHours: {
        status: "unknown",
        valueText: "待核验",
        sourceRefs: [],
        queriedAt: "2026-09-03T00:00:00Z",
        expiresAt: "2026-09-04T00:00:00Z"
      },
      sourceRefs: [],
      evidenceFingerprint: "evidence_visit",
      queriedAt: "2026-09-03T00:00:00Z",
      expiresAt: "2026-09-04T00:00:00Z",
      scheduleCompatibility: "unknown"
    };
    const enriched = {
      ...partial,
      materialFingerprint: "material_visit_v1",
      openingFactsRefreshStatus: "partial",
      visitFactsBySegment: { segment_visit: visitFact },
      verifiedScheduleConflicts: [{ segmentId: "segment_conflict" }]
    } satisfies ComparisonPlanProjection;
    const stale = {
      ...enriched,
      openingFactsRefreshStatus: "not_started",
      visitFactsBySegment: {},
      verifiedScheduleConflicts: []
    } satisfies ComparisonPlanProjection;

    const seeded = upsertVisibleComparisonPlan(createComparisonPreviewState(), enriched).state;
    const replayedCarrier = upsertVisibleComparisonPlan(seeded, stale).state.plans[0];

    expect(replayedCarrier.openingFactsRefreshStatus).toBe("partial");
    expect(replayedCarrier.visitFactsBySegment).toEqual(enriched.visitFactsBySegment);
    expect(replayedCarrier.verifiedScheduleConflicts).toEqual(enriched.verifiedScheduleConflicts);

    const changedMaterial = upsertVisibleComparisonPlan(seeded, {
      ...stale,
      materialFingerprint: "material_visit_v2"
    }).state.plans[0];
    expect(changedMaterial.openingFactsRefreshStatus).toBe("not_started");
    expect(changedMaterial.visitFactsBySegment).toEqual({});
  });

  test("turn replay preserves same-material opening facts from enriched top-level and current state", () => {
    const enriched = {
      ...partial,
      materialFingerprint: "material_replay",
      openingFactsRefreshStatus: "partial",
      visitFactsBySegment: {
        segment_visit: {
          segmentId: "segment_visit",
          amapPoiId: "B000VISIT",
          visitDate: "2026-10-01",
          refreshStatus: "failed",
          facts: {},
          openingHours: {
            status: "unknown",
            valueText: "待核验",
            sourceRefs: [],
            queriedAt: "2026-09-03T00:00:00Z",
            expiresAt: "2026-09-04T00:00:00Z"
          },
          sourceRefs: [],
          evidenceFingerprint: "evidence_replay",
          queriedAt: "2026-09-03T00:00:00Z",
          expiresAt: "2026-09-04T00:00:00Z",
          scheduleCompatibility: "unknown"
        }
      }
    } satisfies ComparisonPlanProjection;
    const stale = {
      ...enriched,
      openingFactsRefreshStatus: "not_started",
      visitFactsBySegment: {}
    } satisfies ComparisonPlanProjection;
    const carrier = {
      id: "assistant_replay",
      role: "assistant",
      status: "active",
      comparisonProjectionUpdateMode: "replace",
      comparisonProjections: [enriched],
      choiceOptions: [{ id: enriched.choiceId, comparisonProjection: stale }]
    };

    const replayed = comparisonPreviewFromTurns([carrier] as never);
    expect(replayed.plans[0].visitFactsBySegment).toEqual(enriched.visitFactsBySegment);

    const current = upsertVisibleComparisonPlan(createComparisonPreviewState(), enriched).state;
    const replayedFromStaleTurns = comparisonPreviewFromTurns(
      [
        {
          ...carrier,
          comparisonProjections: [stale],
          choiceOptions: [{ id: stale.choiceId, comparisonProjection: stale }]
        }
      ] as never,
      current
    );
    expect(replayedFromStaleTurns.plans[0].openingFactsRefreshStatus).toBe("partial");
    expect(replayedFromStaleTurns.plans[0].visitFactsBySegment).toEqual(enriched.visitFactsBySegment);
  });

  test("first visible partial auto-navigates once and later proposals append without stealing focus", () => {
    const first = upsertVisibleComparisonPlan(createComparisonPreviewState(), partial);
    expect(first.shouldAutoNavigate).toBe(true);
    expect(first.state.autoNavigationCount).toBe(1);
    expect(first.state.mapMode).toBe("plan_comparison_preview");
    expect(first.state.focusedProposalId).toBe(partial.proposalId);

    const focused = focusComparisonPlan(first.state, partial.proposalId);
    const proposal = {
      ...partial,
      proposalId: "proposal_2",
      choiceId: "choice_2",
      activeVersionId: null,
      expectedBaseVersionId: "ver_1",
      isPartial: false,
      status: "complete",
      title: "本地沉浸"
    };
    const second = upsertVisibleComparisonPlan(focused, proposal);
    const duplicate = upsertVisibleComparisonPlan(second.state, { ...partial, routeSummary: "追加路线证据" });

    expect(second.shouldAutoNavigate).toBe(false);
    expect(duplicate.shouldAutoNavigate).toBe(false);
    expect(duplicate.state.autoNavigationCount).toBe(1);
    expect(duplicate.state.plans.map((plan) => plan.proposalId)).toEqual(["partial:ver_1", "proposal_2"]);
    expect(duplicate.state.focusedProposalId).toBe(partial.proposalId);
    expect(duplicate.state.plans[0].colorKey).toBe(stablePlanColorKey(partial.proposalId));
  });

  test("same planning turn rejects a projection from a different portfolio root", () => {
    const seeded = upsertVisibleComparisonPlan(createComparisonPreviewState(), partial).state;
    const foreign = upsertVisibleComparisonPlan(seeded, {
      ...partial,
      rootPortfolioId: "portfolio_foreign",
      proposalId: "proposal_foreign",
      choiceId: "choice_foreign",
      isPartial: false,
      status: "complete"
    });

    expect(foreign.shouldAutoNavigate).toBe(false);
    expect(foreign.state).toBe(seeded);
    expect(foreign.state.plans.map((plan) => plan.proposalId)).toEqual([partial.proposalId]);
  });

  test("a later planning root preserves the earlier cards and makes only the latest root actionable", () => {
    const earlier = {
      ...partial,
      nextAction: "complete_pending_slots" as const,
      nextActionLabel: "补全旧方案"
    };
    const later = {
      ...partial,
      planningSelectionRootTurnId: "root_2",
      rootPortfolioId: "portfolio_2",
      proposalId: "partial:ver_2",
      sourceAssistantTurnId: "assistant_2",
      choiceId: "adopt_partial_2",
      title: "新一轮方案",
      nextAction: "complete_pending_slots" as const,
      nextActionLabel: "补全新方案"
    };

    const first = upsertVisibleComparisonPlan(createComparisonPreviewState(), earlier).state;
    const adoptedEarlier = markComparisonPlanAdopted(first, earlier.proposalId, "ver_adopted_old");
    const transitioned = upsertVisibleComparisonPlan(adoptedEarlier, later).state;

    expect(transitioned.plans.map((plan) => plan.proposalId)).toEqual([earlier.proposalId, later.proposalId]);
    expect(transitioned.planningSelectionRootTurnId).toBe(later.planningSelectionRootTurnId);
    expect(transitioned.rootPortfolioId).toBe(later.rootPortfolioId);
    expect(transitioned.focusedProposalId).toBe(later.proposalId);
    const historicalFocus = focusComparisonPlan(transitioned, earlier.proposalId);
    expect(historicalFocus.mapMode).toBe("plan_overview_preview");
    expect(historicalFocus.isMapReadOnly).toBe(true);
    expect(beginPendingSlotComparison(transitioned, earlier.proposalId)).toBe(transitioned);

    const onAdoptPlan = vi.fn();
    const onOpenPlanDetails = vi.fn();
    render(
      <PlanComparison
        comparison={null}
        preview={transitioned}
        onOpenPlanDetails={onOpenPlanDetails}
        onAdoptPlan={onAdoptPlan}
      />
    );

    const historicalCard = screen.getByLabelText("方案 1：文化深游（部分完成）");
    const currentCard = screen.getByLabelText("方案 2：新一轮方案");
    expect(historicalCard.getAttribute("data-comparison-scope")).toBe("historical");
    expect(within(historicalCard).getByText("历史方案，仅供对比")).not.toBeNull();
    expect(within(historicalCard).queryByRole("button", { name: "补全旧方案" })).toBeNull();
    fireEvent.click(within(historicalCard).getByRole("button", { name: "查看详情" }));
    expect(onOpenPlanDetails).toHaveBeenCalledWith(earlier.proposalId);
    expect(currentCard.getAttribute("data-comparison-scope")).toBe("current");

    fireEvent.click(within(currentCard).getByRole("button", { name: "补全 3 个待选体验" }));
    expect(onAdoptPlan).toHaveBeenCalledTimes(1);
    expect(onAdoptPlan).toHaveBeenCalledWith(expect.objectContaining({ proposalId: later.proposalId }));
  });

  test("card focus and pending-slot comparison are read-only until opaque adoption succeeds", () => {
    const seeded = upsertVisibleComparisonPlan(createComparisonPreviewState(), partial).state;
    const focused = focusComparisonPlan(seeded, partial.proposalId);
    expect(focused.mapMode).toBe("plan_overview_preview");
    expect(focused.isMapReadOnly).toBe(true);
    expect(focused.adoptedProposalId).toBeNull();

    const pending = beginPendingSlotComparison(focused, partial.proposalId);
    expect(pending.mapMode).toBe("pending_slot_comparison");
    expect(pending.isMapReadOnly).toBe(true);

    const adopted = markComparisonPlanAdopted(pending, partial.proposalId, "ver_1");
    expect(adopted.mapMode).toBe("itinerary_edit");
    expect(adopted.isMapReadOnly).toBe(false);
    expect(adopted.adoptedProposalId).toBe(partial.proposalId);
  });

  test("colors are identity-stable and pending slots use authoritative day/time without inventing a clock", () => {
    expect(stablePlanColorKey("proposal_2")).toBe(stablePlanColorKey("proposal_2"));
    expect(stablePlanColorKey("proposal_2")).not.toBe(stablePlanColorKey("proposal_3"));
    expect(sortPendingSlots([...partial.pendingSlots]).map((slot) => slot.planningSlotId)).toEqual([
      "art",
      "unknown",
      "heritage"
    ]);
  });

  test("visible plans receive collision-free stable colors and replay preserves user focus", () => {
    let state = createComparisonPreviewState();
    for (const proposalId of ["proposal_1", "proposal_9", "proposal_17", "proposal_25", "proposal_33"]) {
      state = upsertVisibleComparisonPlan(state, { ...partial, proposalId, choiceId: `choice_${proposalId}` }).state;
    }
    expect(new Set(state.plans.map((plan) => plan.colorKey)).size).toBe(5);
    state = focusComparisonPlan(state, "proposal_9");
    const replayed = comparisonPreviewFromTurns(
      [
        {
          id: "assistant_1",
          role: "assistant",
          status: "active",
          choiceOptions: state.plans.map((plan) => ({ id: plan.choiceId, comparisonProjection: plan }))
        }
      ] as never,
      state
    );
    expect(replayed.focusedProposalId).toBe("proposal_9");
    expect(replayed.autoNavigationCount).toBe(1);
    expect(replayed.mapMode).toBe("plan_overview_preview");
  });

  test("the first four proposal colors maximize perceptual hue separation and survive replay", () => {
    let state = createComparisonPreviewState();
    for (const proposalId of ["proposal_a", "proposal_b", "proposal_c", "proposal_d"]) {
      state = upsertVisibleComparisonPlan(state, {
        ...partial,
        proposalId,
        choiceId: `choice_${proposalId}`,
        sourceAssistantTurnId: "assistant_colors",
        rootPortfolioId: "portfolio_colors",
        planningSelectionRootTurnId: "root_colors"
      }).state;
    }

    const colors = state.plans.map((plan) => plan.colorKey);
    const distances = colors.flatMap((left, index) =>
      colors.slice(index + 1).map((right) => perceptualPlanColorDistance(left, right))
    );
    expect(new Set(colors).size).toBe(4);
    expect(Math.min(...distances)).toBeGreaterThanOrEqual(70);

    const replayed = comparisonPreviewFromTurns(
      [
        {
          id: "assistant_colors",
          role: "assistant",
          status: "active",
          choiceOptions: state.plans.map((plan) => ({ id: plan.choiceId, comparisonProjection: plan }))
        }
      ] as never,
      state
    );
    expect(replayed.plans.map((plan) => plan.colorKey)).toEqual(colors);
  });

  test("streamed A/B/C projections replay with persisted choices without losing focus, order, or colors", () => {
    const projections = ["proposal_a", "proposal_b", "proposal_c"].map((proposalId) => ({
      ...partial,
      proposalId,
      choiceId: `choice_${proposalId}`,
      title: `Plan ${proposalId.slice(-1).toUpperCase()}`,
      sourceAssistantTurnId: "assistant_stream_root",
      rootPortfolioId: "portfolio_stream",
      planningSelectionRootTurnId: "root_stream",
      colorKey: stablePlanColorKey(proposalId)
    }));
    let streamed = createComparisonPreviewState();
    for (const projection of projections) {
      streamed = upsertVisibleComparisonPlan(streamed, projection).state;
    }
    streamed = focusComparisonPlan(streamed, "proposal_b");

    const replayed = comparisonPreviewFromTurns(
      [
        {
          id: "assistant_stream_root",
          role: "assistant",
          status: "active",
          planningSteps: projections.map((projection) => ({ metadata: { comparisonProjection: projection } })),
          choiceOptions: projections.map((projection) => ({
            id: projection.choiceId,
            action: "adopt_plan_proposal",
            kind: "plan_proposal",
            label: projection.title,
            comparisonProjection: projection
          }))
        }
      ] as never,
      streamed
    );

    expect(replayed.plans.map((plan) => plan.proposalId)).toEqual(["proposal_a", "proposal_b", "proposal_c"]);
    expect(replayed.focusedProposalId).toBe("proposal_b");
    expect(replayed.autoNavigationCount).toBe(1);
    expect(replayed.plans.map((plan) => plan.colorKey)).toEqual(streamed.plans.map((plan) => plan.colorKey));
  });
  test("final opaque choices prune streamed candidates excluded by Pareto selection", () => {
    const visible = { ...partial, proposalId: "proposal_visible", choiceId: "choice_visible" };
    const ghost = {
      ...partial,
      proposalId: "proposal_ghost",
      choiceId: "choice_ghost",
      isPartial: false,
      adoptionReady: false
    };
    const replayed = comparisonPreviewFromTurns([
      {
        id: "assistant_1",
        role: "assistant",
        status: "active",
        planningSteps: [{ metadata: { comparisonProjection: ghost } }, { metadata: { comparisonProjection: visible } }],
        choiceOptions: [{ id: visible.choiceId, comparisonProjection: visible }]
      }
    ] as never);
    expect(replayed.plans.map((plan) => plan.proposalId)).toEqual(["proposal_visible"]);
  });

  test("latest persisted choices replace earlier Pareto membership for the same planning root", () => {
    const visible = { ...partial, proposalId: "proposal_visible", choiceId: "choice_visible" };
    const ghost = { ...partial, proposalId: "proposal_ghost", choiceId: "choice_ghost", isPartial: false };
    const replayed = comparisonPreviewFromTurns([
      {
        id: "assistant_early",
        role: "assistant",
        status: "active",
        planningSteps: [{ metadata: { comparisonProjection: ghost } }],
        choiceOptions: [
          { id: visible.choiceId, comparisonProjection: visible },
          { id: ghost.choiceId, comparisonProjection: ghost }
        ]
      },
      {
        comparisonProjections: [visible],
        id: "assistant_final",
        role: "assistant",
        status: "active",
        planningSteps: [{ metadata: { comparisonProjection: ghost } }],
        choiceOptions: [{ id: visible.choiceId, comparisonProjection: visible }]
      }
    ] as never);

    expect(replayed.plans.map((plan) => plan.proposalId)).toEqual([visible.proposalId]);
  });

  test("an explicit expansion delta appends a new proposal without replacing the visible partial", () => {
    const visiblePartial = { ...partial };
    const newProposal = {
      ...partial,
      proposalId: "proposal_local_immersion",
      choiceId: "choice_local_immersion",
      sourceAssistantTurnId: "assistant_expansion",
      status: "complete",
      isPartial: false,
      adoptionReady: true,
      activeVersionId: undefined,
      expectedBaseVersionId: "ver_1",
      title: "本地沉浸"
    };
    const replayed = comparisonPreviewFromTurns([
      {
        id: "assistant_initial_partial",
        role: "assistant",
        status: "active",
        comparisonProjections: [visiblePartial],
        choiceOptions: [{ id: visiblePartial.choiceId, comparisonProjection: visiblePartial }]
      },
      {
        id: "assistant_expansion",
        role: "assistant",
        status: "active",
        comparisonProjectionUpdateMode: "append",
        comparisonProjections: [newProposal],
        choiceOptions: [{ id: newProposal.choiceId, comparisonProjection: newProposal }],
        structuredChoiceTrace: {
          sourceAssistantTurnId: "assistant_initial_partial",
          sourceAssistantTurnRole: "assistant",
          sourceAssistantTurnStatus: "active",
          requestChoiceId: "choice_more_plans",
          persistedChoiceId: "choice_more_plans",
          resolvedChoiceId: "choice_more_plans",
          executionChoiceId: "choice_more_plans",
          planningSelectionRootTurnId: visiblePartial.planningSelectionRootTurnId,
          rootPortfolioId: visiblePartial.rootPortfolioId,
          executionAction: "retry_model_planning",
          executionRoute: "controller_choice_resume",
          executionStatus: "succeeded",
          outcome: {
            reason: "new_route_pending_proposal",
            versionDelta: 0,
            patchDelta: 0,
            routeWriteDelta: 0
          }
        }
      }
    ] as never);

    expect(replayed.plans.map((plan) => plan.proposalId)).toEqual([visiblePartial.proposalId, newProposal.proposalId]);
  });

  test("a later read-only partial preview does not replace an earlier verified expansion", () => {
    const initialPartial = { ...partial };
    const verifiedProposal = {
      ...partial,
      proposalId: "proposal_local_immersion",
      choiceId: "choice_local_immersion",
      sourceAssistantTurnId: "assistant_verified_expansion",
      status: "complete",
      isPartial: false,
      adoptionReady: true,
      activeVersionId: undefined,
      title: "本地文化沉浸"
    };
    const readOnlyPartial = {
      ...partial,
      proposalId: "partial-preview:portfolio_1:brief_food",
      choiceId: "preview_partial_portfolio_1_brief_food",
      sourceAssistantTurnId: "assistant_partial_expansion",
      status: "partial_preview",
      isPartial: true,
      adoptionReady: false,
      activeVersionId: null,
      title: "京味美食串联"
    };
    const replayed = comparisonPreviewFromTurns([
      {
        id: "assistant_initial_partial",
        role: "assistant",
        status: "active",
        comparisonProjections: [{ ...initialPartial, sourceAssistantTurnId: "assistant_initial_partial" }],
        choiceOptions: [
          {
            id: initialPartial.choiceId,
            comparisonProjection: {
              ...initialPartial,
              sourceAssistantTurnId: "assistant_initial_partial"
            }
          }
        ]
      },
      {
        id: "assistant_verified_expansion",
        role: "assistant",
        status: "active",
        comparisonProjectionUpdateMode: "append",
        comparisonProjections: [verifiedProposal],
        choiceOptions: [
          {
            id: verifiedProposal.choiceId,
            comparisonProjection: verifiedProposal
          }
        ],
        structuredChoiceTrace: {
          sourceAssistantTurnId: "assistant_initial_partial",
          sourceAssistantTurnRole: "assistant",
          sourceAssistantTurnStatus: "active",
          requestChoiceId: "choice_more_plans_1",
          persistedChoiceId: "choice_more_plans_1",
          resolvedChoiceId: "choice_more_plans_1",
          executionChoiceId: "choice_more_plans_1",
          planningSelectionRootTurnId: initialPartial.planningSelectionRootTurnId,
          rootPortfolioId: initialPartial.rootPortfolioId,
          executionAction: "retry_model_planning",
          executionRoute: "controller_choice_resume",
          executionStatus: "succeeded",
          outcome: {
            reason: "new_verified_proposal",
            versionDelta: 0,
            patchDelta: 0,
            routeWriteDelta: 0
          }
        }
      },
      {
        id: "assistant_partial_expansion",
        role: "assistant",
        status: "active",
        comparisonProjectionUpdateMode: "append",
        comparisonProjections: [
          {
            ...initialPartial,
            sourceAssistantTurnId: "assistant_partial_expansion"
          },
          readOnlyPartial
        ],
        choiceOptions: [
          {
            id: initialPartial.choiceId,
            comparisonProjection: {
              ...initialPartial,
              sourceAssistantTurnId: "assistant_partial_expansion"
            }
          }
        ],
        structuredChoiceTrace: {
          sourceAssistantTurnId: "assistant_verified_expansion",
          sourceAssistantTurnRole: "assistant",
          sourceAssistantTurnStatus: "active",
          requestChoiceId: "choice_more_plans_2",
          persistedChoiceId: "choice_more_plans_2",
          resolvedChoiceId: "choice_more_plans_2",
          executionChoiceId: "choice_more_plans_2",
          planningSelectionRootTurnId: initialPartial.planningSelectionRootTurnId,
          rootPortfolioId: initialPartial.rootPortfolioId,
          executionAction: "retry_model_planning",
          executionRoute: "controller_choice_resume",
          executionStatus: "succeeded",
          outcome: {
            reason: "new_grounded_partial_preview",
            qualifiedPartialProjectionIds: [readOnlyPartial.proposalId],
            partialProjectionDelta: 1,
            versionDelta: 0,
            patchDelta: 0,
            routeWriteDelta: 0
          }
        }
      }
    ] as never);

    expect(replayed.plans.map((plan) => plan.proposalId)).toEqual([
      initialPartial.proposalId,
      verifiedProposal.proposalId,
      readOnlyPartial.proposalId
    ]);
  });

  test("an append carrier cannot rewrite the immutable current draft from an earlier turn", () => {
    const initialDraft = {
      ...partial,
      proposalId: "partial:portfolio_1",
      choiceId: "portfolio_partial_adopt_portfolio_1_",
      sourceAssistantTurnId: "assistant_food_led",
      activeVersionId: null,
      expectedBaseVersionId: null,
      adoptionReady: false,
      title: "地方饮食与街区方向｜北京真实地点草案",
      routeSummary: "首轮真实地点应保留"
    };
    const appended = {
      ...partial,
      proposalId: "partial-preview:portfolio_1:fallback_2_photo_night",
      choiceId: "preview_partial_portfolio_1_fallback_2_photo_night",
      sourceAssistantTurnId: "assistant_photo_night",
      activeVersionId: null,
      expectedBaseVersionId: null,
      adoptionReady: false,
      title: "光影夜游方向｜北京真实地点草案"
    };
    const corruptedCarrier = {
      ...appended,
      proposalId: initialDraft.proposalId,
      choiceId: initialDraft.choiceId
    };

    const replayed = comparisonPreviewFromTurns([
      {
        id: initialDraft.sourceAssistantTurnId,
        role: "assistant",
        status: "active",
        comparisonProjectionUpdateMode: "replace",
        comparisonProjections: [initialDraft],
        choiceOptions: [{ id: initialDraft.choiceId, comparisonProjection: initialDraft }]
      },
      {
        id: appended.sourceAssistantTurnId,
        role: "assistant",
        status: "active",
        comparisonProjectionUpdateMode: "append",
        comparisonProjections: [appended, corruptedCarrier],
        choiceOptions: [
          { id: appended.choiceId, comparisonProjection: appended },
          { id: corruptedCarrier.choiceId, comparisonProjection: corruptedCarrier }
        ]
      }
    ] as never);

    expect(replayed.plans.map((plan) => plan.proposalId)).toEqual([initialDraft.proposalId, appended.proposalId]);
    expect(replayed.plans.map((plan) => plan.title)).toEqual([initialDraft.title, appended.title]);
    expect(replayed.plans[0].routeSummary).toBe("首轮真实地点应保留");
    expect(replayed.plans[0].sourceAssistantTurnId).toBe("assistant_food_led");
  });

  test("legacy successful expansion traces append instead of replacing the prior comparison card", () => {
    const visiblePartial = { ...partial };
    const newProposal = {
      ...partial,
      proposalId: "proposal_local_immersion",
      choiceId: "choice_local_immersion",
      sourceAssistantTurnId: "assistant_expansion",
      status: "complete",
      isPartial: false,
      adoptionReady: true,
      activeVersionId: undefined,
      expectedBaseVersionId: "ver_1",
      title: "本地沉浸"
    };
    const replayed = comparisonPreviewFromTurns([
      {
        id: "assistant_initial_partial",
        role: "assistant",
        status: "active",
        comparisonProjections: [visiblePartial],
        choiceOptions: [{ id: visiblePartial.choiceId, comparisonProjection: visiblePartial }]
      },
      {
        id: "assistant_expansion",
        role: "assistant",
        status: "active",
        comparisonProjections: [newProposal],
        choiceOptions: [{ id: newProposal.choiceId, comparisonProjection: newProposal }],
        structuredChoiceTrace: {
          sourceAssistantTurnId: "assistant_initial_partial",
          sourceAssistantTurnRole: "assistant",
          sourceAssistantTurnStatus: "active",
          requestChoiceId: "choice_more_plans",
          persistedChoiceId: "choice_more_plans",
          resolvedChoiceId: "choice_more_plans",
          executionChoiceId: "choice_more_plans",
          planningSelectionRootTurnId: visiblePartial.planningSelectionRootTurnId,
          rootPortfolioId: visiblePartial.rootPortfolioId,
          executionAction: "retry_model_planning",
          executionRoute: "controller_choice_resume",
          executionStatus: "succeeded",
          outcome: {
            reason: "new_route_pending_proposal",
            proposalDelta: 1,
            versionDelta: 0,
            patchDelta: 0,
            routeWriteDelta: 0
          }
        }
      }
    ] as never);

    expect(replayed.plans.map((plan) => plan.proposalId)).toEqual([visiblePartial.proposalId, newProposal.proposalId]);
  });

  test("server append mode preserves prior cards when a legacy persisted partial execution was misclassified", () => {
    const prior = { ...partial };
    const appended = {
      ...partial,
      proposalId: "partial-preview:portfolio_1:brief_nature",
      choiceId: "preview_partial_portfolio_1_brief_nature",
      sourceAssistantTurnId: "assistant_legacy_append",
      activeVersionId: null,
      adoptionReady: false
    };
    const replayed = comparisonPreviewFromTurns([
      {
        id: "assistant_prior",
        role: "assistant",
        status: "active",
        comparisonProjections: [prior],
        choiceOptions: [{ id: prior.choiceId, comparisonProjection: prior }]
      },
      {
        id: "assistant_legacy_append",
        role: "assistant",
        status: "active",
        comparisonProjectionUpdateMode: "append",
        comparisonProjections: [appended],
        choiceOptions: [{ id: appended.choiceId, comparisonProjection: appended }],
        structuredChoiceTrace: {
          executionStatus: "failed_retryable",
          outcome: { reason: "no_verified_proposal_delta" }
        }
      }
    ] as never);

    expect(replayed.plans.map((plan) => plan.proposalId)).toEqual([prior.proposalId, appended.proposalId]);
  });

  test("recorded portfolio continuations retain every accepted card and ignore the rejected duplicate turn", () => {
    const currentDraft = { ...partial };
    const acceptedBriefs = ["nature_relaxed", "citywalk_hidden_gems", "family_light", "classic"];
    const accepted = acceptedBriefs.map((brief, index) => ({
      ...partial,
      proposalId: "partial-preview:portfolio_1:" + brief,
      choiceId: "preview_partial_portfolio_1_" + brief,
      sourceAssistantTurnId: "assistant_append_" + (index + 1),
      activeVersionId: null,
      adoptionReady: false
    }));
    const rejected = {
      ...partial,
      proposalId: "partial-preview:portfolio_1:photo_night",
      choiceId: "preview_partial_portfolio_1_photo_night",
      sourceAssistantTurnId: "assistant_duplicate",
      activeVersionId: null,
      adoptionReady: false
    };
    const turns = [
      {
        id: "assistant_initial",
        role: "assistant",
        status: "active",
        comparisonProjectionUpdateMode: "replace",
        comparisonProjections: [currentDraft],
        choiceOptions: [{ id: currentDraft.choiceId, comparisonProjection: currentDraft }]
      },
      ...accepted.map((projection) => ({
        id: projection.sourceAssistantTurnId,
        role: "assistant",
        status: "active",
        comparisonProjectionUpdateMode: "append",
        comparisonProjections: [projection],
        choiceOptions: [{ id: projection.choiceId, comparisonProjection: projection }]
      })),
      {
        id: "assistant_duplicate",
        role: "assistant",
        status: "active",
        comparisonProjectionUpdateMode: null,
        comparisonProjections: [rejected],
        choiceOptions: [{ id: rejected.choiceId, comparisonProjection: rejected }]
      }
    ];

    const replayed = comparisonPreviewFromTurns(turns as never);

    expect(replayed.plans.map((plan) => plan.proposalId)).toEqual([
      currentDraft.proposalId,
      ...accepted.map((plan) => plan.proposalId)
    ]);

    const focused = focusComparisonPlan(replayed, accepted[1].proposalId);
    const refreshed = comparisonPreviewFromTurns(turns as never, focused);
    expect(refreshed.plans.map((plan) => plan.proposalId)).toEqual(replayed.plans.map((plan) => plan.proposalId));
    expect(refreshed.plans.map((plan) => plan.colorKey)).toEqual(replayed.plans.map((plan) => plan.colorKey));
    expect(new Set(refreshed.plans.map((plan) => plan.colorKey)).size).toBe(5);
    expect(refreshed.focusedProposalId).toBe(accepted[1].proposalId);

    const onFocusPlan = vi.fn();
    render(<PlanComparison comparison={null} preview={refreshed} onFocusPlan={onFocusPlan} onAdoptPlan={vi.fn()} />);
    expect(
      refreshed.plans.map((plan, index) => screen.getByLabelText(`方案 ${index + 1}：${plan.title}`))
    ).toHaveLength(5);
    fireEvent.click(screen.getByLabelText(`方案 5：${refreshed.plans[4].title}`));
    expect(onFocusPlan).toHaveBeenCalledWith(refreshed.plans[4].proposalId);
  });

  test("a seventh attempted card cannot evict any of the six server-visible cards", () => {
    const visible = Array.from({ length: 6 }, (_, index) => ({
      ...partial,
      proposalId: "proposal_visible_" + (index + 1),
      choiceId: "choice_visible_" + (index + 1),
      sourceAssistantTurnId: "assistant_visible_" + (index + 1)
    }));
    const attempted = {
      ...partial,
      proposalId: "proposal_hidden_7",
      choiceId: "choice_hidden_7",
      sourceAssistantTurnId: "assistant_hidden_7"
    };
    const turns = [
      {
        id: visible[0].sourceAssistantTurnId,
        role: "assistant",
        status: "active",
        comparisonProjectionUpdateMode: "replace",
        comparisonProjections: [visible[0]],
        choiceOptions: [{ id: visible[0].choiceId, comparisonProjection: visible[0] }]
      },
      ...visible.slice(1).map((projection) => ({
        id: projection.sourceAssistantTurnId,
        role: "assistant",
        status: "active",
        comparisonProjectionUpdateMode: "append",
        comparisonProjections: [projection],
        choiceOptions: [{ id: projection.choiceId, comparisonProjection: projection }]
      })),
      {
        id: attempted.sourceAssistantTurnId,
        role: "assistant",
        status: "active",
        comparisonProjectionUpdateMode: null,
        comparisonProjections: [attempted],
        choiceOptions: [{ id: attempted.choiceId, comparisonProjection: attempted }]
      }
    ];

    const replayed = comparisonPreviewFromTurns(turns as never);

    expect(replayed.plans.map((plan) => plan.proposalId)).toEqual(visible.map((plan) => plan.proposalId));
  });

  test("legacy expansion traces without zero-write proof fail closed to replacement", () => {
    const prior = { ...partial };
    const replacement = {
      ...partial,
      proposalId: "proposal_replacement",
      choiceId: "choice_replacement",
      isPartial: false,
      status: "complete"
    };
    const replayed = comparisonPreviewFromTurns([
      {
        id: "assistant_prior",
        role: "assistant",
        status: "active",
        comparisonProjections: [prior],
        choiceOptions: [{ id: prior.choiceId, comparisonProjection: prior }]
      },
      {
        id: "assistant_unproven_expansion",
        role: "assistant",
        status: "active",
        comparisonProjections: [replacement],
        choiceOptions: [{ id: replacement.choiceId, comparisonProjection: replacement }],
        structuredChoiceTrace: {
          sourceAssistantTurnId: "assistant_prior",
          sourceAssistantTurnRole: "assistant",
          sourceAssistantTurnStatus: "active",
          requestChoiceId: "choice_more_plans",
          persistedChoiceId: "choice_more_plans",
          resolvedChoiceId: "choice_more_plans",
          executionChoiceId: "choice_more_plans",
          planningSelectionRootTurnId: prior.planningSelectionRootTurnId,
          rootPortfolioId: prior.rootPortfolioId,
          executionAction: "retry_model_planning",
          executionRoute: "controller_choice_resume",
          executionStatus: "succeeded",
          outcome: {
            reason: "new_verified_proposal",
            versionDelta: 1,
            patchDelta: 0,
            routeWriteDelta: 0
          }
        }
      }
    ] as never);

    expect(replayed.plans.map((plan) => plan.proposalId)).toEqual([replacement.proposalId]);
  });

  test("failed or stale-source expansion turns cannot append comparison cards", () => {
    const prior = { ...partial };
    const attempted = {
      ...partial,
      proposalId: "proposal_attempted",
      choiceId: "choice_attempted",
      isPartial: false,
      status: "complete"
    };
    const replayed = comparisonPreviewFromTurns([
      {
        id: "assistant_prior",
        role: "assistant",
        status: "active",
        comparisonProjections: [prior],
        choiceOptions: [{ id: prior.choiceId, comparisonProjection: prior }]
      },
      {
        id: "assistant_failed_expansion",
        role: "assistant",
        status: "failed",
        comparisonProjectionUpdateMode: "append",
        comparisonProjections: [attempted],
        choiceOptions: [{ id: attempted.choiceId, comparisonProjection: attempted }]
      }
    ] as never);

    expect(replayed.plans.map((plan) => plan.proposalId)).toEqual([prior.proposalId]);
  });

  test("complete projections without a persisted final choice are not replayed", () => {
    const ghost = {
      ...partial,
      proposalId: "proposal_complete_ghost",
      choiceId: "choice_complete_ghost",
      isPartial: false,
      adoptionReady: true
    };
    const replayed = comparisonPreviewFromTurns([
      {
        id: "assistant_complete_ghost",
        role: "assistant",
        status: "active",
        comparisonProjections: [ghost],
        planningSteps: [{ metadata: { comparisonProjection: ghost } }],
        choiceOptions: []
      }
    ] as never);

    expect(replayed.plans).toEqual([]);
  });

  test("a later authoritative turn with no final choices clears earlier complete membership", () => {
    const ghost = {
      ...partial,
      proposalId: "proposal_complete_ghost",
      choiceId: "choice_complete_ghost",
      isPartial: false,
      adoptionReady: true
    };
    const replayed = comparisonPreviewFromTurns([
      {
        id: "assistant_early",
        role: "assistant",
        status: "active",
        comparisonProjections: [ghost],
        choiceOptions: [{ id: ghost.choiceId, comparisonProjection: ghost }]
      },
      {
        id: "assistant_final",
        role: "assistant",
        status: "active",
        comparisonProjections: [ghost],
        choiceOptions: []
      }
    ] as never);

    expect(replayed.plans).toEqual([]);
  });
  test("superseded choice traces cannot mark a visible plan as adopted", () => {
    const visible = { ...partial, proposalId: "proposal_visible", choiceId: "choice_visible" };
    const replayed = comparisonPreviewFromTurns([
      {
        id: "assistant_1",
        role: "assistant",
        status: "active",
        choiceOptions: [{ id: visible.choiceId, comparisonProjection: visible }]
      },
      {
        id: "assistant_superseded",
        role: "assistant",
        status: "superseded",
        itineraryVersionId: "ver_wrong",
        structuredChoiceTrace: {
          executionStatus: "succeeded",
          sourceAssistantTurnId: "assistant_1",
          resolvedChoiceId: visible.choiceId,
          resultVersionId: "ver_wrong"
        }
      }
    ] as never);

    expect(replayed.adoptedProposalId).toBeNull();
    expect(replayed.isMapReadOnly).toBe(true);
  });

  test("active user choice traces restore adopted comparison state after refresh", () => {
    const visible = { ...partial, proposalId: "proposal_visible", choiceId: "choice_visible" };
    const replayed = comparisonPreviewFromTurns([
      {
        id: "assistant_1",
        role: "assistant",
        status: "active",
        choiceOptions: [{ id: visible.choiceId, comparisonProjection: visible }]
      },
      {
        id: "user_choice",
        role: "user",
        status: "active",
        itineraryVersionId: "ver_adopted",
        structuredChoiceTrace: {
          executionStatus: "succeeded",
          sourceAssistantTurnId: "assistant_1",
          resolvedChoiceId: visible.choiceId,
          resultVersionId: "ver_adopted"
        }
      }
    ] as never);

    expect(replayed.adoptedProposalId).toBe(visible.proposalId);
    expect(replayed.adoptedVersionId).toBe("ver_adopted");
    expect(replayed.isMapReadOnly).toBe(false);
  });

  test("a foreign portfolio trace cannot adopt a same-id plan from the active portfolio", () => {
    const visible = { ...partial, proposalId: "proposal_shared", choiceId: "choice_visible" };
    const foreign = {
      ...visible,
      rootPortfolioId: "portfolio_foreign",
      sourceAssistantTurnId: "assistant_foreign",
      choiceId: "choice_foreign"
    };
    const replayed = comparisonPreviewFromTurns([
      {
        id: "assistant_active",
        role: "assistant",
        status: "active",
        choiceOptions: [{ id: visible.choiceId, comparisonProjection: visible }]
      },
      {
        id: "assistant_foreign",
        role: "assistant",
        status: "active",
        comparisonProjectionUpdateMode: "append",
        comparisonProjections: [foreign],
        choiceOptions: [{ id: foreign.choiceId, comparisonProjection: foreign }]
      },
      {
        id: "user_foreign_choice",
        role: "user",
        status: "active",
        itineraryVersionId: "ver_foreign",
        structuredChoiceTrace: {
          executionStatus: "succeeded",
          sourceAssistantTurnId: "assistant_foreign",
          resolvedChoiceId: foreign.choiceId,
          resultVersionId: "ver_foreign"
        }
      }
    ] as never);

    expect(replayed.plans.map((plan) => plan.rootPortfolioId)).toEqual([visible.rootPortfolioId]);
    expect(replayed.adoptedProposalId).toBeNull();
    expect(replayed.adoptedVersionId).toBeNull();
    expect(replayed.isMapReadOnly).toBe(true);
  });

  test("an unrelated persisted option cannot adopt a projection with a different choice id", () => {
    const visible = { ...partial, proposalId: "proposal_visible", choiceId: "choice_visible" };
    const replayed = comparisonPreviewFromTurns([
      {
        id: "assistant_visible",
        role: "assistant",
        status: "active",
        comparisonProjections: [visible],
        choiceOptions: [{ id: "choice_foreign", comparisonProjection: visible }]
      },
      {
        id: "user_foreign_choice",
        role: "user",
        status: "active",
        itineraryVersionId: "ver_foreign",
        structuredChoiceTrace: {
          executionStatus: "succeeded",
          sourceAssistantTurnId: "assistant_visible",
          resolvedChoiceId: "choice_foreign",
          resultVersionId: "ver_foreign"
        }
      }
    ] as never);

    expect(replayed.adoptedProposalId).toBeNull();
    expect(replayed.adoptedVersionId).toBeNull();
    expect(replayed.plans).toEqual([]);
  });

  test("malformed projection payloads fail closed without crashing replay", () => {
    expect(comparisonProjectionFromUnknown({ ...partial, pendingSlots: [{}] })).toBeNull();
    expect(comparisonProjectionFromUnknown({ ...partial, proposalId: {} })).toBeNull();
    expect(comparisonProjectionFromUnknown({ ...partial, days: {} })).toBeNull();
    expect(comparisonProjectionFromUnknown({ ...partial, routeEvidence: {} })).toBeNull();
    expect(comparisonProjectionFromUnknown({ ...partial, days: [{}] })).toBeNull();
    expect(
      comparisonProjectionFromUnknown({
        ...partial,
        days: [{ ...partial.days[0], segments: [{}] }]
      })
    ).toBeNull();
    expect(comparisonProjectionFromUnknown({ ...partial, routeEvidence: [{}] })).toBeNull();
    expect(comparisonProjectionFromUnknown({ ...partial, isPartial: "false" })).toBeNull();
    expect(comparisonProjectionFromUnknown({ ...partial, adoptionReady: "false" })).toBeNull();
    expect(comparisonProjectionFromUnknown({ ...partial, confirmationPassed: "true" })).toBeNull();
    expect(comparisonProjectionFromUnknown({ ...partial, requiredPlanningDayNumbers: [1, "2"] })).toBeNull();
    expect(comparisonProjectionFromUnknown({ ...partial, explicitRestDayNumbers: [0] })).toBeNull();
    expect(comparisonProjectionFromUnknown({ ...partial, uncoveredDayNumbers: {} })).toBeNull();
    const { isPartial: _isPartial, ...missingPartial } = partial;
    const { adoptionReady: _adoptionReady, ...missingAdoptionReady } = partial;
    expect(comparisonProjectionFromUnknown(missingPartial)).toBeNull();
    expect(comparisonProjectionFromUnknown(missingAdoptionReady)).toBeNull();

    const replay = () =>
      comparisonPreviewFromTurns([
        {
          id: "assistant_malformed",
          role: "assistant",
          status: "active",
          comparisonProjections: {},
          choiceOptions: [{ id: "malformed", comparisonProjection: { ...partial, pendingSlots: [{}] } }]
        }
      ] as never);
    expect(replay).not.toThrow();
    expect(replay().plans).toEqual([]);
  });

  test("round-trips the required-day, explicit-rest-day, uncovered-day, and confirmation contract", () => {
    const parsed = comparisonProjectionFromUnknown({
      ...partial,
      workflowMode: "simple_direction_v1",
      confirmationPassed: false,
      requiredPlanningDayNumbers: [2, 1],
      explicitRestDayNumbers: [3],
      uncoveredDayNumbers: [2]
    });

    expect(parsed).toEqual(
      expect.objectContaining({
        confirmationPassed: false,
        requiredPlanningDayNumbers: [1, 2],
        explicitRestDayNumbers: [3],
        uncoveredDayNumbers: [2]
      })
    );

    const legacy = comparisonProjectionFromUnknown(partial);
    expect(legacy).toEqual(
      expect.objectContaining({
        confirmationPassed: undefined,
        requiredPlanningDayNumbers: undefined,
        explicitRestDayNumbers: undefined,
        uncoveredDayNumbers: undefined
      })
    );
  });

  test("round-trips authoritative theme completion state and legacy editable draft mode", () => {
    const parsed = comparisonProjectionFromUnknown({
      ...partial,
      themeEligible: false,
      visibilityMode: "neutral_skeleton",
      partialAdoptionReady: true,
      pendingRatio: 0.4,
      adoptionMode: "editable_draft",
      completionAction: {
        kind: "portfolio_theme_completion",
        theme: "local_food_and_area_walk",
        label: "尝试补全地方饮食与街区",
        choiceId: "theme_completion_choice_1"
      }
    });

    expect(parsed).toEqual(
      expect.objectContaining({
        themeEligible: false,
        visibilityMode: "neutral_skeleton",
        partialAdoptionReady: true,
        pendingRatio: 0.4,
        adoptionMode: "editable_draft",
        completionAction: expect.objectContaining({
          choiceId: "theme_completion_choice_1"
        })
      })
    );
  });
  test("round-trips the authoritative skeleton preview-only visibility mode", () => {
    const parsed = comparisonProjectionFromUnknown({
      ...partial,
      themeEligible: false,
      visibilityMode: "skeleton_preview_only",
      partialAdoptionReady: false,
      adoptionMode: "preview_only",
      pendingRatio: 0.66
    });

    expect(parsed).toEqual(
      expect.objectContaining({
        visibilityMode: "skeleton_preview_only",
        partialAdoptionReady: false,
        pendingRatio: 0.66
      })
    );
  });

  test("round-trips a full RouteOption comparison DTO and rejects a legacy summary", () => {
    const route = {
      id: "route_day_1_a_b",
      planId: "plan_1",
      proposalId: "partial:ver_1",
      fromSegmentId: "segment_a",
      toSegmentId: "segment_b",
      fromPoiId: "poi_a",
      toPoiId: "poi_b",
      fromAmapId: "B0000000001",
      toAmapId: "B0000000002",
      provider: "amap-webservice",
      mode: "transit",
      label: "公交/地铁",
      isSelected: true,
      sortOrder: 1,
      transportMode: "transit",
      distanceMeters: 1800,
      distanceKm: 1.8,
      durationSeconds: 720,
      durationMinutes: 12,
      costAmount: 3,
      costCurrency: "CNY",
      costEstimate: 3,
      crowdingRisk: "medium",
      source: "amap-webservice",
      polyline: [
        [116.3, 39.9],
        [116.31, 39.91]
      ],
      steps: [{ instruction: "步行至地铁站" }],
      providerPayload: { route: "sanitized" },
      error: null,
      queriedAt: "2026-08-01T00:00:00+00:00",
      status: "verified",
      routeStatus: "verified",
      dayNumber: 1,
      candidateFingerprint: "fingerprint_1"
    };
    const parsed = comparisonProjectionFromUnknown({ ...partial, routeEvidence: [route] });

    expect(parsed?.routeEvidence[0]).toEqual(
      expect.objectContaining({
        id: route.id,
        fromSegmentId: route.fromSegmentId,
        toSegmentId: route.toSegmentId,
        durationSeconds: route.durationSeconds,
        polyline: route.polyline,
        provider: route.provider,
        source: route.source
      })
    );
    const legacySummary = { ...route, id: "", polyline: [], provider: "" };
    expect(comparisonProjectionFromUnknown({ ...partial, routeEvidence: [legacySummary] })).toBeNull();
  });

  test("a grounded read-only partial direction survives replay without an adoption choice", () => {
    const visible = { ...partial, proposalId: "proposal_visible", choiceId: "choice_visible" };
    const readOnlyPartial = {
      ...partial,
      proposalId: "partial-preview:portfolio_1:fallback_2_local_immersive",
      choiceId: "preview_partial_portfolio_1_fallback_2_local_immersive",
      status: "partial_preview",
      isPartial: true,
      isAdopted: false,
      adoptionReady: false,
      activeVersionId: null,
      expectedBaseVersionId: "ver_1",
      title: "本地沉浸（只读部分方案）",
      sourceAssistantTurnId: "assistant_1"
    };
    const replayed = comparisonPreviewFromTurns([
      {
        id: "assistant_1",
        role: "assistant",
        status: "active",
        comparisonProjections: [readOnlyPartial, visible],
        planningSteps: [
          { metadata: { comparisonProjection: readOnlyPartial } },
          { metadata: { comparisonProjection: visible } }
        ],
        choiceOptions: [{ id: visible.choiceId, comparisonProjection: visible }],
        structuredChoiceTrace: {
          sourceAssistantTurnId: "assistant_source",
          sourceAssistantTurnRole: "assistant",
          sourceAssistantTurnStatus: "active",
          requestChoiceId: "choice_more_plans",
          persistedChoiceId: "choice_more_plans",
          resolvedChoiceId: "choice_more_plans",
          executionChoiceId: "choice_more_plans",
          planningSelectionRootTurnId: readOnlyPartial.planningSelectionRootTurnId,
          rootPortfolioId: readOnlyPartial.rootPortfolioId,
          executionAction: "retry_model_planning",
          executionRoute: "controller_choice_resume",
          executionStatus: "succeeded",
          outcome: {
            reason: "new_grounded_partial_preview",
            qualifiedPartialProjectionIds: [readOnlyPartial.proposalId],
            partialProjectionDelta: 1,
            versionDelta: 0,
            patchDelta: 0,
            routeWriteDelta: 0
          }
        }
      }
    ] as never);

    expect(replayed.plans.map((plan) => plan.proposalId)).toEqual([readOnlyPartial.proposalId, visible.proposalId]);
    expect(replayed.plans[0]).toEqual(
      expect.objectContaining({
        isPartial: true,
        adoptionReady: false,
        activeVersionId: null
      })
    );
  });

  test("persisted turn projections survive the terminal response when only the active plan has a choice", () => {
    const visible = { ...partial, proposalId: "partial:portfolio_1", choiceId: "choice_visible" };
    const readOnlyPartial = {
      ...partial,
      proposalId: "partial-preview:portfolio_1:fallback_2_local_immersion",
      choiceId: "preview_partial_portfolio_1_fallback_2_local_immersion",
      status: "partial_preview",
      isPartial: true,
      isAdopted: false,
      adoptionReady: false,
      activeVersionId: null,
      expectedBaseVersionId: "ver_1",
      title: "本地沉浸（只读部分方案）",
      sourceAssistantTurnId: "assistant_expansion"
    };

    const replayed = comparisonPreviewFromTurns([
      {
        id: "assistant_expansion",
        role: "assistant",
        status: "active",
        comparisonProjections: [visible, readOnlyPartial],
        planningSteps: [],
        choiceOptions: [{ id: visible.choiceId, comparisonProjection: visible }],
        structuredChoiceTrace: {
          sourceAssistantTurnId: "assistant_source",
          sourceAssistantTurnRole: "assistant",
          sourceAssistantTurnStatus: "active",
          requestChoiceId: "choice_more_plans",
          persistedChoiceId: "choice_more_plans",
          resolvedChoiceId: "choice_more_plans",
          executionChoiceId: "choice_more_plans",
          planningSelectionRootTurnId: readOnlyPartial.planningSelectionRootTurnId,
          rootPortfolioId: readOnlyPartial.rootPortfolioId,
          executionAction: "retry_model_planning",
          executionRoute: "controller_choice_resume",
          executionStatus: "succeeded",
          outcome: {
            reason: "new_verified_partial_preview",
            qualifiedPartialProjectionIds: [readOnlyPartial.proposalId],
            partialProjectionDelta: 1,
            versionDelta: 0,
            patchDelta: 0,
            routeWriteDelta: 0
          }
        }
      }
    ] as never);

    expect(replayed.plans.map((plan) => plan.proposalId)).toEqual([visible.proposalId, readOnlyPartial.proposalId]);
    expect(replayed.plans[1]).toEqual(
      expect.objectContaining({
        isPartial: true,
        adoptionReady: false,
        activeVersionId: null
      })
    );
  });
  test("card focus is preview-only and an actionable card emits its persisted opaque identity", () => {
    const onFocusPlan = vi.fn();
    const onAdoptPlan = vi.fn();
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const actionablePartial = {
      ...partial,
      nextAction: "complete_pending_slots" as const,
      nextActionLabel: "补全 3 个待选体验"
    };
    const preview = upsertVisibleComparisonPlan(createComparisonPreviewState(), actionablePartial).state;
    render(<PlanComparison comparison={null} preview={preview} onFocusPlan={onFocusPlan} onAdoptPlan={onAdoptPlan} />);
    fireEvent.click(screen.getByLabelText("方案 1：文化深游（部分完成）"));
    expect(onFocusPlan).toHaveBeenCalledWith("partial:ver_1");
    expect(fetchMock).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "补全 3 个待选体验" }));
    expect(onAdoptPlan).toHaveBeenCalledWith(
      expect.objectContaining({
        sourceAssistantTurnId: "assistant_1",
        choiceId: "adopt_partial_1"
      })
    );
  });

  test("accepts only complete non-negative comparison summaries and keeps them scoped to the active root", () => {
    const summary = {
      adoptionReadyCount: 1,
      repairablePartialCount: 2,
      remainingQualifiedEntityCount: 4,
      frontierStatus: "has_more" as const,
      lastOutcomeReason: "candidate_collision_frontier_remaining"
    };
    expect(comparisonSummaryFromUnknown(summary)).toEqual(expect.objectContaining(summary));
    expect(comparisonSummaryFromUnknown({ ...summary, remainingQualifiedEntityCount: -1 })).toBeNull();
    expect(comparisonSummaryFromUnknown({ ...summary, frontierStatus: "made_up" })).toBeNull();

    const activeTurn = {
      id: "assistant_summary_active",
      role: "assistant",
      status: "active",
      comparisonProjectionUpdateMode: "replace",
      comparisonProjections: [partial],
      choiceOptions: [{ id: partial.choiceId, comparisonProjection: partial }],
      comparisonSummary: summary
    };
    const foreignSummaryTurn = {
      id: "assistant_summary_foreign",
      role: "assistant",
      status: "internal_capability",
      comparisonSummary: { ...summary, adoptionReadyCount: 9 },
      structuredChoiceTrace: {
        planningSelectionRootTurnId: "root_foreign",
        rootPortfolioId: "portfolio_foreign"
      }
    };

    const replayed = comparisonPreviewFromTurns([activeTurn, foreignSummaryTurn] as never);
    expect(replayed.comparisonSummary).toEqual(expect.objectContaining(summary));
    expect(replayed.comparisonSummary?.adoptionReadyCount).toBe(1);
  });

  test.each(Array.from({ length: 30 }, (_, index) => index))(
    "fixed recorded preview/adoption/three-slot state path remains stable #%s",
    (iteration) => {
      let state = upsertVisibleComparisonPlan(createComparisonPreviewState(), {
        ...partial,
        proposalId: `partial:ver_${iteration}`,
        activeVersionId: `ver_${iteration}_0`,
        expectedBaseVersionId: `ver_${iteration}_0`,
        pendingSlots: [
          { planningSlotId: "slot_art", dayNumber: 1, timeWindow: "14:00-18:00", displayNeed: "art_walk" },
          { planningSlotId: "slot_campus_day2", dayNumber: 2, timeWindow: "09:00-12:00", displayNeed: "高校参观" },
          { planningSlotId: "slot_heritage", dayNumber: 2, timeWindow: "14:00-18:00", displayNeed: "heritage_walk" }
        ]
      }).state;
      expect(state.autoNavigationCount).toBe(1);
      const initialProposalId = state.plans[0].proposalId;
      const cardPreview = focusComparisonPlan(state, initialProposalId);
      expect(cardPreview.isMapReadOnly).toBe(true);
      const laterPlan = upsertVisibleComparisonPlan(cardPreview, {
        ...partial,
        planningSelectionRootTurnId: state.planningSelectionRootTurnId ?? "",
        rootPortfolioId: state.rootPortfolioId ?? "",
        proposalId: `proposal_later_${iteration}`,
        choiceId: `choice_later_${iteration}`,
        activeVersionId: null,
        expectedBaseVersionId: `ver_${iteration}_0`,
        title: "后续追加方案"
      });
      expect(laterPlan.shouldAutoNavigate).toBe(false);
      expect(laterPlan.state.focusedProposalId).toBe(initialProposalId);
      state = laterPlan.state;
      state = markComparisonPlanAdopted(state, state.plans[0].proposalId, `ver_${iteration}_0`);
      expect(state.isMapReadOnly).toBe(false);
      expect(state.mapMode).toBe("itinerary_edit");

      const sequence = [3];
      for (const remaining of [2, 1, 0]) {
        const projection = {
          ...state.plans[0],
          activeVersionId: `ver_${iteration}_${3 - remaining}`,
          expectedBaseVersionId: `ver_${iteration}_${3 - remaining}`,
          pendingSlots: state.plans[0].pendingSlots.slice(1)
        };
        state = upsertVisibleComparisonPlan(state, projection).state;
        sequence.push(state.plans[0].pendingSlots.length);
      }
      const duplicate = upsertVisibleComparisonPlan(state, state.plans[0]);
      const frontendMetrics = {
        iteration: iteration + 1,
        comparisonAutoNavigated: duplicate.state.autoNavigationCount > 0,
        autoNavigationCount: duplicate.state.autoNavigationCount,
        firstVisiblePlanBlockedByLaterPlans: laterPlan.state.focusedProposalId !== initialProposalId,
        cardPreviewReadOnly: cardPreview.isMapReadOnly,
        cardPreviewVersionPatchRouteDelta: [0, 0, 0],
        prematureMapEditEnabled: !cardPreview.isMapReadOnly,
        exactAdoptionSucceeded:
          duplicate.state.adoptedProposalId === initialProposalId &&
          duplicate.state.adoptedVersionId === `ver_${iteration}_0`,
        adoptedMapMode: duplicate.state.mapMode,
        pendingSlotCountSequence: sequence,
        colorMappingMismatch: duplicate.state.plans[0].colorKey !== stablePlanColorKey(`partial:ver_${iteration}`)
      };
      expect(duplicate.shouldAutoNavigate).toBe(false);
      expect(duplicate.state.autoNavigationCount).toBe(1);
      expect(sequence).toEqual([3, 2, 1, 0]);
      expect(duplicate.state.plans[0].colorKey).toBe(stablePlanColorKey(`partial:ver_${iteration}`));
      console.log(`TRIP_FRONTEND_STABILITY_METRICS=${JSON.stringify(frontendMetrics)}`);
    }
  );
});

describe("PlanComparison card interaction contract", () => {
  test("a day block focuses that exact proposal day without falling through to the card default", () => {
    const onFocusPlan = vi.fn();
    const onFocusPlanDay = vi.fn();
    const dayOne: ComparisonPlanProjection["days"][number] = {
      id: "day_1",
      dayNumber: 1,
      title: "Day 1",
      weatherSummary: "待查询",
      riskSummary: "待核验",
      totalEstimatedCost: 0,
      segments: [
        {
          id: "segment_day_1",
          startTime: "09:00",
          endTime: "10:00",
          kind: "activity",
          poi: {
            id: "poi_day_1",
            amapId: "amap_day_1",
            name: "第一天验证地点",
            city: "北京",
            category: "landmark",
            latitude: 39.9,
            longitude: 116.4,
            source: "amap",
            confidence: 0.9
          },
          transportMode: "transit",
          estimatedCost: 0,
          notes: ""
        }
      ]
    };
    const twoDayPlan = {
      ...partial,
      days: [
        dayOne,
        {
          ...dayOne,
          id: "day_2",
          dayNumber: 2,
          segments: dayOne.segments.map((segment) => ({
            ...segment,
            id: `${segment.id}_day_2`,
            poi: { ...segment.poi, id: `${segment.poi.id}_day_2`, name: "第二天验证地点" }
          }))
        }
      ]
    };
    const preview = upsertVisibleComparisonPlan(createComparisonPreviewState(), twoDayPlan).state;

    render(
      <PlanComparison
        comparison={null}
        focusedDayNumber={2}
        preview={preview}
        onFocusPlan={onFocusPlan}
        onFocusPlanDay={onFocusPlanDay}
      />
    );

    const dayTwo = screen.getByRole("button", { name: "查看方案 1 Day 2 地图" });
    expect(dayTwo.getAttribute("aria-pressed")).toBe("true");
    fireEvent.click(dayTwo);

    expect(onFocusPlanDay).toHaveBeenCalledTimes(1);
    expect(onFocusPlanDay).toHaveBeenCalledWith(twoDayPlan.proposalId, 2);
    expect(onFocusPlan).not.toHaveBeenCalled();
  });

  test("a superseded Simple direction stays read-only while only the current verified direction is confirmable", () => {
    const onFocusPlan = vi.fn();
    const onOpenPlanDetails = vi.fn();
    const onAdoptPlan = vi.fn();
    const directionA = {
      ...partial,
      workflowMode: "simple_direction_v1" as const,
      nextAction: "confirm_edit" as const,
      nextActionLabel: "确认编辑",
      confirmationPassed: true,
      requiredPlanningDayNumbers: [1],
      explicitRestDayNumbers: [],
      uncoveredDayNumbers: [],
      days: simpleReadyDays(),
      ...simpleMealProjectionEvidence(),
      routeStatus: "route_ready",
      routeExpectedLegCount: 2,
      routeVerifiedLegCount: 2,
      proposalId: "direction_a",
      choiceId: "confirm_a",
      title: "高校集中方向"
    };
    const directionB = {
      ...directionA,
      planningSelectionRootTurnId: "root_2",
      rootPortfolioId: "portfolio_2",
      proposalId: "direction_b",
      sourceAssistantTurnId: "assistant_2",
      choiceId: "confirm_b",
      title: "公园夜游方向"
    };
    const withA = upsertVisibleComparisonPlan(createComparisonPreviewState(), directionA).state;
    const state = upsertVisibleComparisonPlan(withA, directionB).state;

    render(
      <PlanComparison
        comparison={null}
        preview={state}
        onFocusPlan={onFocusPlan}
        onOpenPlanDetails={onOpenPlanDetails}
        onAdoptPlan={onAdoptPlan}
      />
    );

    const historicalCard = screen.getByLabelText("方案 1：高校集中方向");
    const currentCard = screen.getByLabelText("方案 2：公园夜游方向");
    expect(historicalCard.getAttribute("data-comparison-scope")).toBe("historical");
    expect(within(historicalCard).queryByRole("button", { name: "确认编辑「高校集中方向」" })).toBeNull();
    expect(within(currentCard).getByRole("button", { name: "确认编辑「公园夜游方向」" })).not.toBeNull();

    fireEvent.click(within(historicalCard).getByRole("button", { name: "查看详情" }));
    expect(onOpenPlanDetails).toHaveBeenCalledWith("direction_a");
    expect(focusComparisonPlan(state, "direction_a")).toEqual(
      expect.objectContaining({ mapMode: "plan_overview_preview", isMapReadOnly: true })
    );

    fireEvent.click(within(currentCard).getByRole("button", { name: "确认编辑「公园夜游方向」" }));
    expect(onAdoptPlan).toHaveBeenCalledTimes(1);
    expect(onAdoptPlan).toHaveBeenCalledWith(
      expect.objectContaining({ sourceAssistantTurnId: "assistant_2", choiceId: "confirm_b" })
    );

    const rejectedHistoricalAdoption = markComparisonPlanAdopted(state, "direction_a", "ver_direction_a");
    expect(rejectedHistoricalAdoption).toBe(state);
    expect(rejectedHistoricalAdoption.adoptedProposalId).toBeNull();
    expect(rejectedHistoricalAdoption.adoptedVersionId).toBeNull();
    expect(rejectedHistoricalAdoption.mapMode).toBe("plan_comparison_preview");
    expect(rejectedHistoricalAdoption.isMapReadOnly).toBe(true);
    expect(focusComparisonPlan(rejectedHistoricalAdoption, "direction_a")).toEqual(
      expect.objectContaining({ mapMode: "plan_overview_preview", isMapReadOnly: true })
    );
  });

  test("a route-degraded Simple partial is repair-only even when an obsolete confirm identity is present", () => {
    const onAdoptPlan = vi.fn();
    const onRepairPlan = vi.fn();
    const routeDegradedPartial = {
      ...partial,
      workflowMode: "simple_direction_v1" as const,
      status: "partial",
      isPartial: true,
      adoptionReady: true,
      draftAdoptionReady: true,
      adoptionMode: "editable_partial" as const,
      currentReadiness: "route_pending" as const,
      routeStatus: "route_provider_failed",
      routeExpectedLegCount: 2,
      routeVerifiedLegCount: 0,
      confirmationPassed: false,
      blockingReasons: [],
      nextAction: "confirm_edit" as const,
      nextActionLabel: "确认编辑",
      sourceAssistantTurnId: "assistant_partial_ready",
      choiceId: "confirm_partial_ready",
      repairChoiceId: "repair_partial_ready"
    };
    const state = upsertVisibleComparisonPlan(createComparisonPreviewState(), routeDegradedPartial).state;

    render(<PlanComparison comparison={null} preview={state} onAdoptPlan={onAdoptPlan} onRepairPlan={onRepairPlan} />);

    const card = screen.getByLabelText(`方案 1：${routeDegradedPartial.title}`);
    expect(within(card).queryByRole("button", { name: /^确认编辑/ })).toBeNull();
    const repair = within(card).getByRole("button", { name: "补全此方案" });
    expect(repair.getAttribute("data-choice-id")).toBe("repair_partial_ready");
    fireEvent.click(repair);
    expect(onRepairPlan).toHaveBeenCalledWith(expect.objectContaining({ repairChoiceId: "repair_partial_ready" }), 0);
    expect(onAdoptPlan).not.toHaveBeenCalled();
  });

  test.each([
    {
      label: "verifier readiness is false",
      patch: { adoptionReady: false },
      expectedStatus: "暂不可确认：方案采用条件待核验"
    },
    {
      label: "the readiness contract is blocked",
      patch: {
        adoptionReady: false,
        adoptionMode: "blocked" as const,
        currentReadiness: "blocked" as const
      },
      expectedStatus: "暂不可确认：方案采用条件待核验"
    },
    {
      label: "semantic verification failed",
      patch: { blockingReasons: ["semantic_coverage_failed"] },
      expectedStatus: "暂不可确认：地点语义校验未通过"
    },
    {
      label: "the next action is not confirm-edit",
      patch: { nextAction: "continue_editing" as const },
      expectedStatus: "暂不可确认：方案采用条件待核验"
    },
    {
      label: "the source assistant identity is missing",
      patch: { sourceAssistantTurnId: " " },
      expectedStatus: "暂不可确认：方案采用条件待核验"
    },
    {
      label: "the opaque choice identity is missing",
      patch: { choiceId: " " },
      expectedStatus: "暂不可确认：方案采用条件待核验"
    }
  ])("does not render an executable Simple confirm action when $label", ({ patch, expectedStatus }) => {
    const onAdoptPlan = vi.fn();
    const blockedDirection = {
      ...partial,
      workflowMode: "simple_direction_v1" as const,
      adoptionReady: true,
      adoptionMode: "editable_partial" as const,
      currentReadiness: "route_pending" as const,
      blockingReasons: [],
      nextAction: "confirm_edit" as const,
      nextActionLabel: "确认编辑",
      sourceAssistantTurnId: "assistant_ready",
      choiceId: "confirm_ready",
      ...patch
    };
    const state = upsertVisibleComparisonPlan(createComparisonPreviewState(), blockedDirection).state;

    render(<PlanComparison comparison={null} preview={state} onAdoptPlan={onAdoptPlan} />);

    const card = screen.getByLabelText(`方案 1：${blockedDirection.title}`);
    expect(within(card).queryByRole("button", { name: /^确认编辑/ })).toBeNull();
    expect(within(card).getByRole("status").textContent).toBe(expectedStatus);
    expect(onAdoptPlan).not.toHaveBeenCalled();
  });

  test("save-back replaces a direction snapshot in place and refreshes its opaque capability", () => {
    const directionA = {
      ...partial,
      workflowMode: "simple_direction_v1" as const,
      nextAction: "confirm_edit" as const,
      nextActionLabel: "确认编辑",
      proposalId: "direction_a",
      choiceId: "confirm_a",
      title: "高校集中方向"
    };
    const directionB = {
      ...directionA,
      proposalId: "direction_b",
      choiceId: "confirm_b",
      title: "公园夜游方向"
    };
    let state = upsertVisibleComparisonPlan(createComparisonPreviewState(), directionA).state;
    state = upsertVisibleComparisonPlan(state, directionB).state;
    const originalColor = state.plans[0].colorKey;
    const saved = updateComparisonPlanSnapshot(state, {
      ...directionA,
      sourceAssistantTurnId: "assistant_saved_a",
      choiceId: "confirm_a_saved",
      activeVersionId: "ver_a_edited",
      title: "高校集中方向（已编辑）"
    });

    expect(saved.plans.map((plan) => plan.proposalId)).toEqual(["direction_a", "direction_b"]);
    expect(saved.plans[0]).toEqual(
      expect.objectContaining({
        title: "高校集中方向（已编辑）",
        activeVersionId: "ver_a_edited",
        sourceAssistantTurnId: "assistant_saved_a",
        choiceId: "confirm_a_saved",
        colorKey: originalColor
      })
    );
    expect(saved.plans[1]).toEqual(state.plans[1]);
  });

  test("append only adds a new Simple direction while an internal replace refreshes existing card capabilities", () => {
    const directionA = {
      ...partial,
      workflowMode: "simple_direction_v1" as const,
      nextAction: "confirm_edit" as const,
      nextActionLabel: "确认编辑",
      proposalId: "direction_a",
      choiceId: "confirm_a_0",
      sourceAssistantTurnId: "assistant_initial",
      title: "高校集中方向"
    };
    const directionB = {
      ...directionA,
      proposalId: "direction_b",
      choiceId: "confirm_b_0",
      sourceAssistantTurnId: "assistant_append",
      title: "公园夜游方向"
    };
    const illegalAppendRefresh = {
      ...directionA,
      choiceId: "confirm_a_illegal_append",
      sourceAssistantTurnId: "assistant_append",
      title: "高校集中方向（不应由 append 覆盖）"
    };
    const authoritativeA = {
      ...directionA,
      choiceId: "confirm_a_1",
      sourceAssistantTurnId: "assistant_replace",
      title: "高校集中方向（已保存）"
    };
    const authoritativeB = {
      ...directionB,
      choiceId: "confirm_b_1",
      sourceAssistantTurnId: "assistant_replace"
    };
    const initialTurn = {
      id: "assistant_initial",
      role: "assistant",
      status: "active",
      comparisonProjectionUpdateMode: "replace",
      comparisonProjections: [directionA],
      choiceOptions: [{ id: directionA.choiceId, comparisonProjection: directionA }]
    };
    const appendTurn = {
      id: "assistant_append",
      role: "assistant",
      status: "active",
      comparisonProjectionUpdateMode: "append",
      comparisonProjections: [directionB, illegalAppendRefresh],
      choiceOptions: [
        { id: directionB.choiceId, comparisonProjection: directionB },
        { id: illegalAppendRefresh.choiceId, comparisonProjection: illegalAppendRefresh }
      ]
    };
    const replaceTurn = {
      id: "assistant_replace",
      role: "assistant",
      status: "internal_capability",
      comparisonProjectionUpdateMode: "replace",
      comparisonProjections: [authoritativeA, authoritativeB],
      choiceOptions: [
        { id: authoritativeA.choiceId, comparisonProjection: authoritativeA },
        { id: authoritativeB.choiceId, comparisonProjection: authoritativeB }
      ]
    };

    const appendedOnly = comparisonPreviewFromTurns([initialTurn, appendTurn] as never);
    expect(appendedOnly.plans.map((plan) => plan.proposalId)).toEqual(["direction_a", "direction_b"]);
    expect(appendedOnly.plans[0]).toEqual(
      expect.objectContaining({
        title: "高校集中方向",
        sourceAssistantTurnId: "assistant_initial",
        choiceId: "confirm_a_0"
      })
    );

    const replayed = comparisonPreviewFromTurns([initialTurn, appendTurn, replaceTurn] as never);

    expect(replayed.plans.map((plan) => plan.proposalId)).toEqual(["direction_a", "direction_b"]);
    expect(replayed.plans[0]).toEqual(
      expect.objectContaining({
        title: "高校集中方向（已保存）",
        sourceAssistantTurnId: "assistant_replace",
        choiceId: "confirm_a_1"
      })
    );
    expect(replayed.plans[1]).toEqual(
      expect.objectContaining({
        title: "公园夜游方向",
        sourceAssistantTurnId: "assistant_replace",
        choiceId: "confirm_b_1"
      })
    );
  });

  test("a blocked Simple replace stays visible as evidence and a later signed replace upgrades the same card", () => {
    const blocked = {
      ...partial,
      workflowMode: "simple_direction_v1" as const,
      proposalId: "direction_blocked_then_ready",
      sourceAssistantTurnId: "assistant_blocked",
      choiceId: "non_executable_projection_identity",
      status: "partial",
      isPartial: true,
      adoptionReady: false,
      draftAdoptionReady: false,
      partialAdoptionReady: false,
      adoptionMode: "blocked" as const,
      currentReadiness: "blocked" as const,
      blockingReasons: ["simple_direction_verified_semantic_mismatch"],
      blockingReasonLabels: ["夜景必选地点尚未满足"],
      nextAction: "verify_routes_and_adopt" as const,
      nextActionLabel: "重新补充缺失地点",
      title: "高校与夜景方向（待补充）"
    };
    const blockedTurn = {
      id: "assistant_blocked",
      role: "assistant",
      status: "active",
      comparisonProjectionUpdateMode: "replace",
      comparisonProjections: [blocked],
      choiceOptions: []
    };

    const blockedPreview = comparisonPreviewFromTurns([blockedTurn] as never);

    expect(blockedPreview.plans).toHaveLength(1);
    expect(blockedPreview.plans[0]).toEqual(
      expect.objectContaining({
        planningSelectionRootTurnId: blocked.planningSelectionRootTurnId,
        rootPortfolioId: blocked.rootPortfolioId,
        proposalId: blocked.proposalId,
        adoptionReady: false,
        blockingReasons: ["simple_direction_verified_semantic_mismatch"]
      })
    );
    const onAdoptPlan = vi.fn();
    const view = render(<PlanComparison comparison={null} preview={blockedPreview} onAdoptPlan={onAdoptPlan} />);
    const blockedCard = screen.getByLabelText(`方案 1：${blocked.title}`);
    expect(blockedCard.getAttribute("data-adoption-ready")).toBe("false");
    expect(within(blockedCard).queryByRole("button", { name: /^确认编辑/ })).toBeNull();
    expect(within(blockedCard).getByRole("status").textContent).toBe("暂不可确认：夜景必选地点尚未满足");
    expect(onAdoptPlan).not.toHaveBeenCalled();

    const ready = {
      ...blocked,
      sourceAssistantTurnId: "assistant_ready",
      choiceId: "confirm_ready",
      adoptionReady: true,
      confirmationPassed: true,
      requiredPlanningDayNumbers: [1],
      explicitRestDayNumbers: [],
      uncoveredDayNumbers: [],
      days: simpleReadyDays(),
      ...simpleMealProjectionEvidence(),
      draftAdoptionReady: true,
      partialAdoptionReady: true,
      adoptionMode: "editable_partial" as const,
      currentReadiness: "route_pending" as const,
      blockingReasons: [],
      blockingReasonLabels: [],
      routeStatus: "route_ready",
      routeExpectedLegCount: 2,
      routeVerifiedLegCount: 2,
      nextAction: "confirm_edit" as const,
      nextActionLabel: "确认编辑",
      title: "高校与夜景方向（可编辑）"
    };
    const readyTurn = {
      id: "assistant_ready",
      role: "assistant",
      status: "internal_capability",
      comparisonProjectionUpdateMode: "replace",
      comparisonProjections: [ready],
      choiceOptions: [{ id: ready.choiceId, comparisonProjection: ready }]
    };
    const readyPreview = comparisonPreviewFromTurns([blockedTurn, readyTurn] as never);

    expect(readyPreview.plans).toHaveLength(1);
    expect(readyPreview.plans[0]).toEqual(
      expect.objectContaining({
        planningSelectionRootTurnId: blocked.planningSelectionRootTurnId,
        rootPortfolioId: blocked.rootPortfolioId,
        proposalId: blocked.proposalId,
        sourceAssistantTurnId: "assistant_ready",
        choiceId: "confirm_ready",
        adoptionReady: true,
        colorKey: blockedPreview.plans[0].colorKey
      })
    );
    view.rerender(<PlanComparison comparison={null} preview={readyPreview} onAdoptPlan={onAdoptPlan} />);
    const readyCard = screen.getByLabelText(`方案 1：${ready.title}`);
    fireEvent.click(within(readyCard).getByRole("button", { name: "确认编辑「高校与夜景方向（可编辑）」" }));
    expect(onAdoptPlan).toHaveBeenCalledTimes(1);
    expect(onAdoptPlan).toHaveBeenCalledWith(
      expect.objectContaining({ proposalId: blocked.proposalId, choiceId: "confirm_ready" })
    );
  });

  test("single click and Space only focus, while double click, Enter, and 查看详情 open details", () => {
    const onFocusPlan = vi.fn();
    const onOpenPlanDetails = vi.fn();
    const state = upsertVisibleComparisonPlan(createComparisonPreviewState(), partial).state;
    render(
      <PlanComparison
        comparison={null}
        preview={state}
        onFocusPlan={onFocusPlan}
        onOpenPlanDetails={onOpenPlanDetails}
      />
    );

    const card = screen.getByLabelText("方案 1：文化深游（部分完成）");
    fireEvent.click(card);
    expect(onFocusPlan).toHaveBeenCalledTimes(1);
    expect(onOpenPlanDetails).not.toHaveBeenCalled();

    onFocusPlan.mockClear();
    fireEvent.keyDown(card, { key: " " });
    expect(onFocusPlan).toHaveBeenCalledTimes(1);
    expect(onOpenPlanDetails).not.toHaveBeenCalled();

    onFocusPlan.mockClear();
    fireEvent.doubleClick(card);
    expect(onFocusPlan).toHaveBeenCalledTimes(1);
    expect(onOpenPlanDetails).toHaveBeenCalledTimes(1);

    onFocusPlan.mockClear();
    onOpenPlanDetails.mockClear();
    fireEvent.keyDown(card, { key: "Enter" });
    expect(onFocusPlan).toHaveBeenCalledTimes(1);
    expect(onOpenPlanDetails).toHaveBeenCalledTimes(1);

    onFocusPlan.mockClear();
    onOpenPlanDetails.mockClear();
    fireEvent.click(screen.getByRole("button", { name: "查看详情" }));
    expect(onFocusPlan).toHaveBeenCalledTimes(1);
    expect(onOpenPlanDetails).toHaveBeenCalledTimes(1);
  });

  test("action buttons do not bubble click, double-click, or keyboard events to the card", () => {
    const onFocusPlan = vi.fn();
    const onOpenPlanDetails = vi.fn();
    const onAdoptPlan = vi.fn();
    const actionable = {
      ...partial,
      nextAction: "adopt" as const,
      nextActionLabel: "采用此方案"
    };
    const state = upsertVisibleComparisonPlan(createComparisonPreviewState(), actionable).state;
    render(
      <PlanComparison
        comparison={null}
        preview={state}
        onFocusPlan={onFocusPlan}
        onOpenPlanDetails={onOpenPlanDetails}
        onAdoptPlan={onAdoptPlan}
      />
    );

    const action = screen.getByRole("button", { name: "采用此方案" });
    fireEvent.click(action);
    fireEvent.doubleClick(action);
    fireEvent.keyDown(action, { key: "Enter" });
    fireEvent.keyDown(action, { key: " " });

    expect(onAdoptPlan).toHaveBeenCalledTimes(1);
    expect(onFocusPlan).not.toHaveBeenCalled();
    expect(onOpenPlanDetails).not.toHaveBeenCalled();
  });
});

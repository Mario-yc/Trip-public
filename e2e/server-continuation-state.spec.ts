import { expect, test } from "@playwright/test";

import { readLatestServerContinuationState } from "./support/server-continuation-state";

function comparisonTurn(
  id: string,
  frontierStatus: string,
  choiceOptions: Record<string, unknown>[] = [],
) {
  return {
    id,
    planningSelectionRootTurnId: "turn_root",
    rootPortfolioId: "portfolio_root",
    comparisonSummary: {
      frontierStatus,
      blockingLayer:
        frontierStatus === "route_feasible_exhausted" ? "route" : null,
      lastOutcomeReason:
        frontierStatus === "route_feasible_exhausted"
          ? "topology_constraint_exceeded"
          : null,
    },
    choiceOptions,
  };
}

function continuation(id: string, lifecycle = "offered") {
  return {
    id,
    choiceId: id,
    action: "continue_plan_expansion",
    kind: "simple_direction_more_plans",
    lifecycle,
    sourceAssistantTurnId: "turn_latest",
    sourceUserTurnId: "turn_root",
    planningSelectionRootTurnId: "turn_root",
    rootPortfolioId: "portfolio_root",
    requestContractFingerprint: "f".repeat(64),
    workflowMode: "simple_direction_v1",
  };
}

test("returns a terminal state without inventing a continuation capability", () => {
  const state = readLatestServerContinuationState({
    turns: [comparisonTurn("turn_latest", "route_feasible_exhausted")],
  });

  expect(state.capability).toBeNull();
  expect(state.stopReason).toBe("frontier_terminal:route_feasible_exhausted");
  expect(state.summary).toMatchObject({
    frontierStatus: "route_feasible_exhausted",
    blockingLayer: "route",
    lastOutcomeReason: "topology_constraint_exceeded",
  });
});

test("accepts exactly one current server-signed continuation", () => {
  const state = readLatestServerContinuationState({
    turns: [
      comparisonTurn("turn_latest", "has_more", [
        continuation("choice_continue"),
      ]),
    ],
  });

  expect(state.stopReason).toBeNull();
  expect(state.capability).toMatchObject({
    choiceId: "choice_continue",
    sourceAssistantTurnId: "turn_latest",
    planningSelectionRootTurnId: "turn_root",
    rootPortfolioId: "portfolio_root",
    requestContractFingerprint: "f".repeat(64),
  });
});

test("does not resurrect an older capability when the latest comparison turn is terminal", () => {
  const state = readLatestServerContinuationState({
    turns: [
      comparisonTurn("turn_old", "has_more", [
        {
          ...continuation("choice_old"),
          sourceAssistantTurnId: "turn_old",
        },
      ]),
      comparisonTurn("turn_latest", "route_feasible_exhausted"),
    ],
  });

  expect(state.capability).toBeNull();
  expect(state.sourceAssistantTurnId).toBe("turn_latest");
});

test("fails closed for multiple active capabilities", () => {
  expect(() =>
    readLatestServerContinuationState({
      turns: [
        comparisonTurn("turn_latest", "has_more", [
          continuation("choice_a"),
          continuation("choice_b"),
        ]),
      ],
    }),
  ).toThrow("server_continuation_capability_count_invalid");
});

test("reports has-more without a capability as a server contract mismatch", () => {
  const state = readLatestServerContinuationState({
    turns: [comparisonTurn("turn_latest", "has_more")],
  });

  expect(state.capability).toBeNull();
  expect(state.stopReason).toBe(
    "continue_capability_missing_while_frontier_has_more",
  );
});

for (const lifecycle of [
  "executing",
  "cancelled",
  "succeeded",
  "hidden",
  "future_unknown_state",
]) {
  test(`fails closed without returning a capability for ${lifecycle} lifecycle`, () => {
    const state = readLatestServerContinuationState({
      turns: [
        comparisonTurn("turn_latest", "has_more", [
          continuation("choice_continue", lifecycle),
        ]),
      ],
    });

    expect(state.capability).toBeNull();
    expect(state.stopReason).toBe(
      `continue_capability_not_executable:${lifecycle}`,
    );
  });
}

test("accepts an explicitly retryable current server-signed continuation", () => {
  const state = readLatestServerContinuationState({
    turns: [
      comparisonTurn("turn_latest", "has_more", [
        continuation("choice_retryable", "failed_retryable"),
      ]),
    ],
  });

  expect(state.stopReason).toBeNull();
  expect(state.capability).toMatchObject({ choiceId: "choice_retryable" });
});

test("fails closed when canonical choice identities drift", () => {
  expect(() =>
    readLatestServerContinuationState({
      turns: [
        comparisonTurn("turn_latest", "has_more", [
          {
            ...continuation("choice_server"),
            choiceId: "choice_projected_drift",
          },
        ]),
      ],
    }),
  ).toThrow("server_continuation_choice_identity_mismatch");
});

test("fails closed when source user lineage drifts", () => {
  expect(() =>
    readLatestServerContinuationState({
      turns: [
        comparisonTurn("turn_latest", "has_more", [
          {
            ...continuation("choice_continue"),
            sourceUserTurnId: "turn_other",
          },
        ]),
      ],
    }),
  ).toThrow("server_continuation_identity_mismatch");
});

test("fails closed when workflow identity drifts", () => {
  expect(() =>
    readLatestServerContinuationState({
      turns: [
        comparisonTurn("turn_latest", "has_more", [
          {
            ...continuation("choice_continue"),
            workflowMode: "creative_portfolio_v1",
          },
        ]),
      ],
    }),
  ).toThrow("server_continuation_identity_mismatch");
});

test("fails closed when the request fingerprint is not canonical sha256", () => {
  expect(() =>
    readLatestServerContinuationState({
      turns: [
        comparisonTurn("turn_latest", "has_more", [
          {
            ...continuation("choice_continue"),
            requestContractFingerprint: "not-the-frozen-fingerprint",
          },
        ]),
      ],
    }),
  ).toThrow("server_continuation_request_fingerprint_invalid");
});

import { expect, test } from "@playwright/test";

import {
  extractExactSelectedAgentChoice,
  readLatestSimpleDirectionLiveState,
  selectStrictDetourOption,
} from "./support/simple-direction-live-state";

function comparisonTurn(
  id: string,
  frontierStatus: string,
  choiceOptions: Record<string, unknown>[] = [],
) {
  return {
    id,
    planningSelectionRootTurnId: "turn_root",
    rootPortfolioId: "portfolio_root",
    comparisonSummary: { frontierStatus },
    choiceOptions,
  };
}

function retryTurn(
  id: string,
  choiceOptions: Record<string, unknown>[] = [retryChoice("choice_retry", id)],
) {
  return {
    id,
    choiceOptions,
  };
}

function retryChoice(choiceId: string, sourceAssistantTurnId = "turn_retry") {
  return {
    id: choiceId,
    choiceId,
    kind: "safe_fallback_action",
    action: "retry_model_planning",
    lifecycle: "offered",
    sourceAssistantTurnId,
  };
}

function detourQuestion() {
  return {
    dimensionId: "route_decision.detour_tolerance",
    question: "您对绕行和额外行程时间的接受程度如何？",
    allowFreeText: false,
    options: [
      {
        id: "moderate",
        label: "适中",
        semanticValue: {
          detourTolerance: {
            maxGeneralizedCostDelta: 35,
            maxDetourRatio: 0.35,
          },
        },
      },
      {
        id: "strict",
        label: "严格",
        semanticValue: {
          detourTolerance: {
            maxGeneralizedCostDelta: 20,
            maxDetourRatio: 0.2,
          },
        },
      },
    ],
  };
}

test("prefers the latest retry capability over an older comparison summary", () => {
  const state = readLatestSimpleDirectionLiveState({
    turns: [
      comparisonTurn("turn_old", "has_more", [
        {
          id: "choice_continue",
          choiceId: "choice_continue",
          action: "continue_plan_expansion",
          kind: "simple_direction_more_plans",
          lifecycle: "offered",
          sourceAssistantTurnId: "turn_old",
          sourceUserTurnId: "turn_root",
          planningSelectionRootTurnId: "turn_root",
          rootPortfolioId: "portfolio_root",
          requestContractFingerprint: "f".repeat(64),
          workflowMode: "simple_direction_v1",
        },
      ]),
      retryTurn("turn_retry"),
    ],
  });

  expect(state).toEqual({
    kind: "retry_model_planning",
    capability: {
      choiceId: "choice_retry",
      sourceAssistantTurnId: "turn_retry",
    },
  });
});

test("fails closed when retry is not the unique executable fallback on the latest turn", () => {
  expect(() =>
    readLatestSimpleDirectionLiveState({
      turns: [
        retryTurn("turn_retry", [
          retryChoice("choice_retry_a", "turn_retry"),
          retryChoice("choice_retry_b", "turn_retry"),
        ]),
      ],
    }),
  ).toThrow("simple_direction_retry_capability_count_invalid");
});

test("fails closed instead of resurrecting state when the latest turn is a user turn", () => {
  expect(() =>
    readLatestSimpleDirectionLiveState({
      turns: [
        comparisonTurn("turn_old", "exhausted"),
        { id: "turn_user", role: "user" },
      ],
    }),
  ).toThrow("simple_direction_latest_turn_not_assistant");
});

test("returns the comparison continuation state when the latest turn has a summary", () => {
  const state = readLatestSimpleDirectionLiveState({
    turns: [
      comparisonTurn("turn_latest", "has_more", [
        {
          id: "choice_continue",
          choiceId: "choice_continue",
          action: "continue_plan_expansion",
          kind: "simple_direction_more_plans",
          lifecycle: "offered",
          sourceAssistantTurnId: "turn_latest",
          sourceUserTurnId: "turn_root",
          planningSelectionRootTurnId: "turn_root",
          rootPortfolioId: "portfolio_root",
          requestContractFingerprint: "f".repeat(64),
          workflowMode: "simple_direction_v1",
        },
      ]),
    ],
  });

  expect(state.kind).toBe("comparison");
  expect(state.capability).toMatchObject({
    choiceId: "choice_continue",
    sourceAssistantTurnId: "turn_latest",
  });
});

test("extracts only the exact selected agent choice identity from stream payload", () => {
  expect(
    extractExactSelectedAgentChoice({
      context: {
        selectedAgentChoice: {
          sourceAssistantTurnId: "turn_retry",
          choiceId: "choice_retry",
        },
      },
    }),
  ).toEqual({
    sourceAssistantTurnId: "turn_retry",
    choiceId: "choice_retry",
  });
});

test("rejects manual or extra fields in the selected agent choice payload", () => {
  expect(() =>
    extractExactSelectedAgentChoice({
      context: {
        selectedAgentChoice: {
          sourceAssistantTurnId: "turn_retry",
          choiceId: "choice_retry",
          manualValue: "手填",
        },
      },
    }),
  ).toThrow("selected_agent_choice_payload_invalid");
});

test("selects the strict detour option without relying on the untracked helper", () => {
  const selected = selectStrictDetourOption(detourQuestion());

  expect(selected.optionId).toBe("strict");
  expect(selected.submissionMode).toBe("persisted_option");
});

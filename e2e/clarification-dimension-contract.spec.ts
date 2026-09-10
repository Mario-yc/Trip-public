import { expect, test } from "@playwright/test";

import {
  assertLatestClarificationSubmissionSucceeded,
  validateClarificationDimensionIdentitySet,
} from "./support/clarification-dimension-contract";

const EXPECTED_DIMENSIONS = [
  "route_decision.mobility_profile",
  "route_decision.detour_tolerance",
  "night_view.cardinality",
  "night_view.experience_mode",
] as const;

test("fails at the submitted clarification boundary instead of rescanning a retained checkpoint", () => {
  expect(() =>
    assertLatestClarificationSubmissionSucceeded(
      {
        turns: [
          {
            id: "turn_source_route",
            role: "assistant",
            turnIndex: 2,
            status: "active",
            clarificationCheckpoint: {
              checkpointId: "checkpoint_route",
              fingerprint: "checkpoint_fingerprint_route",
              sourceAssistantTurnId: "turn_source_route",
              status: "awaiting_answer",
            },
          },
          {
            id: "turn_submit_user",
            role: "user",
            turnIndex: 3,
            status: "active",
          },
          {
            id: "turn_failed_result",
            role: "assistant",
            turnIndex: 4,
            status: "failed",
            failureReason: "sensitive provider payload must not be echoed",
            structuredChoiceTrace: {
              sourceAssistantTurnId: "turn_source_route",
              checkpointId: "checkpoint_route",
              checkpointFingerprint: "checkpoint_fingerprint_route",
              executionStatus: "failed_retryable",
            },
          },
        ],
      },
      {
        checkpointId: "checkpoint_route",
        checkpointFingerprint: "checkpoint_fingerprint_route",
        sourceAssistantTurnId: "turn_source_route",
      },
    ),
  ).toThrow(
    'clarification_submit_failed:{"sourceAssistantTurnId":"turn_source_route","assistantTurnId":"turn_failed_result","assistantStatus":"failed","executionStatus":"failed_retryable"}',
  );
});

test("returns bounded exact-source evidence for a successful clarification submission", () => {
  expect(
    assertLatestClarificationSubmissionSucceeded(
      {
        turns: [
          {
            id: "turn_source_night",
            role: "assistant",
            turnIndex: 2,
            status: "active",
            clarificationCheckpoint: {
              checkpointId: "checkpoint_night",
              fingerprint: "checkpoint_fingerprint_night",
              sourceAssistantTurnId: "turn_source_night",
            },
          },
          {
            id: "turn_submit_user",
            role: "user",
            turnIndex: 3,
            status: "active",
          },
          {
            id: "turn_next_checkpoint",
            role: "assistant",
            turnIndex: 4,
            status: "active",
            structuredChoiceTrace: {
              sourceAssistantTurnId: "turn_source_night",
              checkpointId: "checkpoint_night",
              checkpointFingerprint: "checkpoint_fingerprint_night",
              executionStatus: "succeeded",
            },
          },
        ],
      },
      {
        checkpointId: "checkpoint_night",
        checkpointFingerprint: "checkpoint_fingerprint_night",
        sourceAssistantTurnId: "turn_source_night",
      },
    ),
  ).toEqual({
    checkpointId: "checkpoint_night",
    checkpointFingerprint: "checkpoint_fingerprint_night",
    sourceAssistantTurnId: "turn_source_night",
    assistantTurnId: "turn_next_checkpoint",
    assistantStatus: "active",
    executionStatus: "succeeded",
  });
});

test("fails closed when the exact-source result trace drifts from the submitted checkpoint ID", () => {
  expect(() =>
    assertLatestClarificationSubmissionSucceeded(
      {
        turns: [
          {
            id: "turn_source",
            role: "assistant",
            turnIndex: 2,
            clarificationCheckpoint: {
              checkpointId: "checkpoint_expected",
              fingerprint: "fingerprint_expected",
              sourceAssistantTurnId: "turn_source",
            },
          },
          {
            id: "turn_result",
            role: "assistant",
            turnIndex: 4,
            status: "active",
            structuredChoiceTrace: {
              sourceAssistantTurnId: "turn_source",
              checkpointId: "checkpoint_drifted",
              checkpointFingerprint: "fingerprint_expected",
              executionStatus: "succeeded",
              failureReason: "sensitive result failure must not be exposed",
              payload: { secret: "must-not-appear" },
            },
          },
        ],
      },
      {
        checkpointId: "checkpoint_expected",
        checkpointFingerprint: "fingerprint_expected",
        sourceAssistantTurnId: "turn_source",
      },
    ),
  ).toThrow(
    'clarification_submit_checkpoint_id_invalid:{"sourceAssistantTurnId":"turn_source","assistantTurnId":"turn_result"}',
  );
});

test("fails closed when the exact-source result trace drifts from the submitted checkpoint fingerprint", () => {
  expect(() =>
    assertLatestClarificationSubmissionSucceeded(
      {
        turns: [
          {
            id: "turn_source",
            role: "assistant",
            turnIndex: 2,
            clarificationCheckpoint: {
              checkpointId: "checkpoint_expected",
              fingerprint: "fingerprint_expected",
              sourceAssistantTurnId: "turn_source",
            },
          },
          {
            id: "turn_result",
            role: "assistant",
            turnIndex: 4,
            status: "active",
            structuredChoiceTrace: {
              sourceAssistantTurnId: "turn_source",
              checkpointId: "checkpoint_expected",
              checkpointFingerprint: "fingerprint_drifted",
              executionStatus: "succeeded",
            },
          },
        ],
      },
      {
        checkpointId: "checkpoint_expected",
        checkpointFingerprint: "fingerprint_expected",
        sourceAssistantTurnId: "turn_source",
      },
    ),
  ).toThrow(
    'clarification_submit_checkpoint_fingerprint_invalid:{"sourceAssistantTurnId":"turn_source","assistantTurnId":"turn_result"}',
  );
});

test("fails closed when the exact-source result trace omits checkpoint identity", () => {
  expect(() =>
    assertLatestClarificationSubmissionSucceeded(
      {
        turns: [
          {
            id: "turn_source",
            role: "assistant",
            turnIndex: 2,
            clarificationCheckpoint: {
              checkpointId: "checkpoint_expected",
              fingerprint: "fingerprint_expected",
              sourceAssistantTurnId: "turn_source",
            },
          },
          {
            id: "turn_result",
            role: "assistant",
            turnIndex: 4,
            status: "active",
            structuredChoiceTrace: {
              sourceAssistantTurnId: "turn_source",
              executionStatus: "succeeded",
            },
          },
        ],
      },
      {
        checkpointId: "checkpoint_expected",
        checkpointFingerprint: "fingerprint_expected",
        sourceAssistantTurnId: "turn_source",
      },
    ),
  ).toThrow(
    'clarification_submit_checkpoint_identity_missing:{"sourceAssistantTurnId":"turn_source","assistantTurnId":"turn_result"}',
  );
});

test("fails closed when the refreshed session has no exact-source result turn", () => {
  expect(() =>
    assertLatestClarificationSubmissionSucceeded(
      {
        turns: [
          {
            id: "turn_source",
            role: "assistant",
            turnIndex: 2,
            status: "active",
            clarificationCheckpoint: {
              checkpointId: "checkpoint_source",
              fingerprint: "checkpoint_fingerprint_source",
              sourceAssistantTurnId: "turn_source",
            },
          },
          {
            id: "turn_unrelated",
            role: "assistant",
            turnIndex: 4,
            status: "active",
            structuredChoiceTrace: {
              sourceAssistantTurnId: "turn_other",
              executionStatus: "succeeded",
            },
          },
        ],
      },
      {
        checkpointId: "checkpoint_source",
        checkpointFingerprint: "checkpoint_fingerprint_source",
        sourceAssistantTurnId: "turn_source",
      },
    ),
  ).toThrow(
    'clarification_submit_lineage_invalid:{"sourceAssistantTurnId":"turn_source","assistantTurnId":"turn_unrelated","traceSourceAssistantTurnId":"turn_other"}',
  );
});

test("fails closed when one submission yields more than one assistant result", () => {
  expect(() =>
    assertLatestClarificationSubmissionSucceeded(
      {
        turns: [
          {
            id: "turn_source",
            role: "assistant",
            turnIndex: 2,
            status: "active",
            clarificationCheckpoint: {
              checkpointId: "checkpoint_source",
              fingerprint: "checkpoint_fingerprint_source",
              sourceAssistantTurnId: "turn_source",
            },
          },
          {
            id: "turn_result_one",
            role: "assistant",
            turnIndex: 4,
            status: "active",
            structuredChoiceTrace: {
              sourceAssistantTurnId: "turn_source",
              executionStatus: "succeeded",
            },
          },
          {
            id: "turn_result_two",
            role: "assistant",
            turnIndex: 5,
            status: "active",
            structuredChoiceTrace: {
              sourceAssistantTurnId: "turn_source",
              executionStatus: "succeeded",
            },
          },
        ],
      },
      {
        checkpointId: "checkpoint_source",
        checkpointFingerprint: "checkpoint_fingerprint_source",
        sourceAssistantTurnId: "turn_source",
      },
    ),
  ).toThrow(
    'clarification_submit_result_count_invalid:{"sourceAssistantTurnId":"turn_source","resultTurnCount":2}',
  );
});

test("accepts the original route-first clarification dimension order", () => {
  expect(
    validateClarificationDimensionIdentitySet(
      [...EXPECTED_DIMENSIONS],
      EXPECTED_DIMENSIONS,
    ),
  ).toEqual([...EXPECTED_DIMENSIONS].sort());
});

test("accepts the live night-first clarification dimension order", () => {
  expect(
    validateClarificationDimensionIdentitySet(
      [
        "night_view.cardinality",
        "night_view.experience_mode",
        "route_decision.mobility_profile",
        "route_decision.detour_tolerance",
      ],
      EXPECTED_DIMENSIONS,
    ),
  ).toEqual([...EXPECTED_DIMENSIONS].sort());
});

for (const [name, dimensions] of [
  [
    "missing dimension",
    [
      "night_view.cardinality",
      "night_view.experience_mode",
      "route_decision.mobility_profile",
    ],
  ],
  [
    "duplicate dimension",
    [
      "night_view.cardinality",
      "night_view.cardinality",
      "route_decision.mobility_profile",
      "route_decision.detour_tolerance",
    ],
  ],
  [
    "unknown dimension",
    [
      "night_view.cardinality",
      "night_view.experience_mode",
      "route_decision.mobility_profile",
      "route_decision.detour_tolerance",
      "route_decision.unknown",
    ],
  ],
] as const) {
  test(`fails closed for ${name}`, () => {
    expect(() =>
      validateClarificationDimensionIdentitySet(
        dimensions,
        EXPECTED_DIMENSIONS,
      ),
    ).toThrow();
  });
}

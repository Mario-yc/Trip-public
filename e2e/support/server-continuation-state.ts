type JsonRecord = Record<string, unknown>;

export type ServerContinuationState = {
  capability: JsonRecord | null;
  summary: JsonRecord;
  sourceAssistantTurnId: string;
  stopReason: string | null;
};

const EXECUTABLE_LIFECYCLES = new Set(["offered", "failed_retryable"]);
const SHA256_FINGERPRINT = /^[0-9a-f]{64}$/i;

export function readLatestServerContinuationState(
  session: JsonRecord,
): ServerContinuationState {
  const turns = asArray(session.turns);
  for (let index = turns.length - 1; index >= 0; index -= 1) {
    const turn = asRecord(turns[index]);
    const summary = asRecord(turn.comparisonSummary);
    if (!Object.keys(summary).length) continue;

    const sourceAssistantTurnId = requiredIdentity(
      turn.id,
      "server_continuation_source_turn_missing",
    );
    const planningSelectionRootTurnId = requiredIdentity(
      turn.planningSelectionRootTurnId,
      "server_continuation_planning_root_missing",
    );
    const rootPortfolioId = requiredIdentity(
      turn.rootPortfolioId,
      "server_continuation_portfolio_missing",
    );
    const choices = asArray(turn.choiceOptions)
      .map((value) => asRecord(value))
      .filter((choice) => choice.action === "continue_plan_expansion");
    if (choices.length > 1) {
      throw new Error("server_continuation_capability_count_invalid");
    }
    if (choices.length === 0) {
      const frontierStatus = String(summary.frontierStatus || "");
      return {
        capability: null,
        summary,
        sourceAssistantTurnId,
        stopReason:
          frontierStatus === "has_more"
            ? "continue_capability_missing_while_frontier_has_more"
            : `frontier_terminal:${frontierStatus || "unknown"}`,
      };
    }

    const choice = choices[0];
    const lifecycle = String(choice.lifecycle || "offered");
    if (!EXECUTABLE_LIFECYCLES.has(lifecycle)) {
      return {
        capability: null,
        summary,
        sourceAssistantTurnId,
        stopReason: `continue_capability_not_executable:${lifecycle}`,
      };
    }
    const choiceId = requiredIdentity(
      choice.id,
      "server_continuation_choice_id_missing",
    );
    const projectedChoiceId = requiredIdentity(
      choice.choiceId,
      "server_continuation_projected_choice_id_missing",
    );
    if (projectedChoiceId !== choiceId) {
      throw new Error("server_continuation_choice_identity_mismatch");
    }
    const sourceUserTurnId = requiredIdentity(
      choice.sourceUserTurnId,
      "server_continuation_source_user_turn_missing",
    );
    const requestContractFingerprint = requiredIdentity(
      choice.requestContractFingerprint,
      "server_continuation_request_fingerprint_missing",
    );
    if (!SHA256_FINGERPRINT.test(requestContractFingerprint)) {
      throw new Error("server_continuation_request_fingerprint_invalid");
    }
    if (
      choice.kind !== "simple_direction_more_plans" ||
      choice.workflowMode !== "simple_direction_v1" ||
      choice.sourceAssistantTurnId !== sourceAssistantTurnId ||
      sourceUserTurnId !== planningSelectionRootTurnId ||
      choice.planningSelectionRootTurnId !== planningSelectionRootTurnId ||
      choice.rootPortfolioId !== rootPortfolioId
    ) {
      throw new Error("server_continuation_identity_mismatch");
    }
    if (String(summary.frontierStatus || "") !== "has_more") {
      throw new Error("server_continuation_frontier_status_mismatch");
    }
    return {
      capability: {
        choiceId,
        lifecycle,
        sourceAssistantTurnId,
        sourceUserTurnId,
        planningSelectionRootTurnId,
        rootPortfolioId,
        requestContractFingerprint,
        workflowMode: "simple_direction_v1",
      },
      summary,
      sourceAssistantTurnId,
      stopReason: null,
    };
  }
  throw new Error("latest_comparison_summary_missing");
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function asRecord(value: unknown): JsonRecord {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as JsonRecord)
    : {};
}

function requiredIdentity(value: unknown, errorCode: string): string {
  const identity = String(value || "");
  if (!identity) throw new Error(errorCode);
  return identity;
}

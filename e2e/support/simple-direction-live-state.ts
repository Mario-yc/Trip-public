type JsonRecord = Record<string, unknown>;

export type CapturedStreamRequest = {
  body: JsonRecord;
  rawBody: string;
  url: string;
};

export type SimpleDirectionLiveState =
  | {
      kind: "retry_model_planning";
      capability: {
        choiceId: string;
        sourceAssistantTurnId: string;
      };
    }
  | {
      kind: "comparison";
      capability: JsonRecord | null;
      summary: JsonRecord;
      sourceAssistantTurnId: string;
      stopReason: string | null;
    };

export type DetourTolerance = {
  maxGeneralizedCostDelta: number;
  maxDetourRatio: number;
};

export type AdjacentLegConstraint = {
  candidateSearchRadiusMeters: number;
  maxProviderTravelMinutes: number;
};

export type DetourSemanticValue = {
  detourTolerance: DetourTolerance;
  adjacentLegConstraint?: AdjacentLegConstraint;
};

export type PersistedDetourOptionSelection = {
  optionId: string;
  label: string;
  semanticValue: DetourSemanticValue;
  submissionMode: "persisted_option";
};

type ValidatedOption = PersistedDetourOptionSelection & {
  ratio: number;
  delta: number;
};

const DETOUR_DIMENSION_ID = "route_decision.detour_tolerance";
const EXECUTABLE_LIFECYCLES = new Set(["offered", "failed_retryable"]);
const SHA256_FINGERPRINT = /^[0-9a-f]{64}$/i;

export function readLatestSimpleDirectionLiveState(
  session: JsonRecord,
): SimpleDirectionLiveState {
  const turns = asArray(session.turns);
  const latestTurn = asRecord(turns.at(-1));
  const sourceAssistantTurnId = requiredIdentity(
    latestTurn.id,
    "simple_direction_latest_turn_missing",
  );
  if (latestTurn.role !== undefined && latestTurn.role !== "assistant") {
    throw new Error("simple_direction_latest_turn_not_assistant");
  }
  const summary = asRecord(latestTurn.comparisonSummary);
  if (Object.keys(summary).length > 0) {
    return readComparisonState(latestTurn, summary, sourceAssistantTurnId);
  }

  const retryChoices = asArray(latestTurn.choiceOptions)
    .map((value) => asRecord(value))
    .filter(
      (choice) =>
        choice.kind === "safe_fallback_action" &&
        choice.action === "retry_model_planning" &&
        EXECUTABLE_LIFECYCLES.has(String(choice.lifecycle || "offered")),
    );
  if (retryChoices.length !== 1) {
    throw new Error("simple_direction_retry_capability_count_invalid");
  }
  const retry = retryChoices[0];
  const choiceId = requiredIdentity(
    retry.id,
    "simple_direction_retry_choice_id_missing",
  );
  if (retry.choiceId !== undefined && retry.choiceId !== choiceId) {
    throw new Error("simple_direction_retry_choice_identity_mismatch");
  }
  if (
    retry.sourceAssistantTurnId !== undefined &&
    retry.sourceAssistantTurnId !== sourceAssistantTurnId
  ) {
    throw new Error("simple_direction_retry_source_turn_mismatch");
  }
  if (retry.allowsManualInput === true) {
    throw new Error("simple_direction_retry_manual_input_forbidden");
  }
  return {
    kind: "retry_model_planning",
    capability: {
      choiceId,
      sourceAssistantTurnId,
    },
  };
}

export function extractExactSelectedAgentChoice(payload: JsonRecord): {
  sourceAssistantTurnId: string;
  choiceId: string;
} {
  const context = asRecord(payload.context);
  const selected = asRecord(context.selectedAgentChoice);
  const keys = Object.keys(selected).sort();
  if (
    keys.length !== 2 ||
    keys[0] !== "choiceId" ||
    keys[1] !== "sourceAssistantTurnId"
  ) {
    throw new Error("selected_agent_choice_payload_invalid");
  }
  return {
    sourceAssistantTurnId: requiredIdentity(
      selected.sourceAssistantTurnId,
      "selected_agent_choice_source_turn_missing",
    ),
    choiceId: requiredIdentity(
      selected.choiceId,
      "selected_agent_choice_id_missing",
    ),
  };
}

function readComparisonState(
  turn: JsonRecord,
  summary: JsonRecord,
  sourceAssistantTurnId: string,
): Extract<SimpleDirectionLiveState, { kind: "comparison" }> {
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
      kind: "comparison",
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
      kind: "comparison",
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
    kind: "comparison",
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

export function selectStrictDetourOption(
  questionValue: unknown,
): PersistedDetourOptionSelection {
  const question = requireRecord(questionValue, "detour question");
  if (question.dimensionId !== DETOUR_DIMENSION_ID) {
    throw new Error("detour question dimension mismatch");
  }
  if (question.allowFreeText !== false) {
    throw new Error("detour question must forbid free text");
  }
  if (!Array.isArray(question.options)) {
    throw new Error("detour options must be an array");
  }
  if (question.options.length < 2 || question.options.length > 3) {
    throw new Error("detour option count out of contract");
  }

  const seenIds = new Set<string>();
  const options = question.options.map((value, index) => {
    const option = validateOption(value, index);
    if (seenIds.has(option.optionId)) {
      throw new Error("detour option identity duplicated");
    }
    seenIds.add(option.optionId);
    return option;
  });
  const selected = [...options].sort((left, right) => {
    if (left.ratio !== right.ratio) return left.ratio - right.ratio;
    if (left.delta !== right.delta) return left.delta - right.delta;
    return left.optionId.localeCompare(right.optionId);
  })[0];
  return {
    optionId: selected.optionId,
    label: selected.label,
    semanticValue: selected.semanticValue,
    submissionMode: "persisted_option",
  };
}

function validateOption(value: unknown, index: number): ValidatedOption {
  const option = requireRecord(value, `detour option ${index}`);
  const optionId = requiredIdentity(
    option.id,
    `detour option ${index} identity`,
  );
  const label = requiredIdentity(option.label, `detour option ${index} label`);
  const semanticValue = requireRecord(
    option.semanticValue,
    `detour option ${index} semanticValue`,
  );
  requireExactKeys(
    semanticValue,
    ["detourTolerance"],
    ["adjacentLegConstraint"],
    `detour option ${index} semanticValue`,
  );
  const tolerance = requireRecord(
    semanticValue.detourTolerance,
    `detour option ${index} detourTolerance`,
  );
  requireExactKeys(
    tolerance,
    ["maxGeneralizedCostDelta", "maxDetourRatio"],
    [],
    `detour option ${index} detourTolerance`,
  );
  const delta = requireFiniteNumber(
    tolerance.maxGeneralizedCostDelta,
    `detour option ${index} maxGeneralizedCostDelta`,
  );
  const ratio = requireFiniteNumber(
    tolerance.maxDetourRatio,
    `detour option ${index} maxDetourRatio`,
  );
  if (delta <= 0) {
    throw new Error("detour maxGeneralizedCostDelta must be positive");
  }
  if (ratio < 0 || ratio > 1) {
    throw new Error("detour maxDetourRatio must be within 0..1");
  }

  const boundedSemanticValue: DetourSemanticValue = {
    detourTolerance: {
      maxGeneralizedCostDelta: delta,
      maxDetourRatio: ratio,
    },
  };
  if (semanticValue.adjacentLegConstraint !== undefined) {
    const adjacent = requireRecord(
      semanticValue.adjacentLegConstraint,
      `detour option ${index} adjacentLegConstraint`,
    );
    requireExactKeys(
      adjacent,
      ["candidateSearchRadiusMeters", "maxProviderTravelMinutes"],
      [],
      `detour option ${index} adjacentLegConstraint`,
    );
    const radius = requireFiniteNumber(
      adjacent.candidateSearchRadiusMeters,
      `detour option ${index} candidateSearchRadiusMeters`,
    );
    const minutes = requireFiniteNumber(
      adjacent.maxProviderTravelMinutes,
      `detour option ${index} maxProviderTravelMinutes`,
    );
    if (radius < 100 || radius > 50_000) {
      throw new Error("detour candidateSearchRadiusMeters out of contract");
    }
    if (minutes < 1 || minutes > 480) {
      throw new Error("detour maxProviderTravelMinutes out of contract");
    }
    boundedSemanticValue.adjacentLegConstraint = {
      candidateSearchRadiusMeters: radius,
      maxProviderTravelMinutes: minutes,
    };
  }
  return {
    optionId,
    label,
    semanticValue: boundedSemanticValue,
    submissionMode: "persisted_option",
    ratio,
    delta,
  };
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function asRecord(value: unknown): JsonRecord {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as JsonRecord)
    : {};
}

function requireRecord(value: unknown, field: string): JsonRecord {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error(`${field} must be an object`);
  }
  return value as JsonRecord;
}

function requiredIdentity(value: unknown, errorCode: string): string {
  if (typeof value !== "string" || !value || value.trim() !== value) {
    throw new Error(errorCode);
  }
  return value;
}

function requireFiniteNumber(value: unknown, field: string): number {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new Error(`${field} must be a finite number`);
  }
  return value;
}

function requireExactKeys(
  value: JsonRecord,
  required: string[],
  optional: string[],
  field: string,
) {
  const keys = Object.keys(value);
  const allowed = new Set([...required, ...optional]);
  if (
    required.some((key) => !Object.prototype.hasOwnProperty.call(value, key)) ||
    keys.some((key) => !allowed.has(key))
  ) {
    throw new Error(`${field} contains missing or unknown fields`);
  }
}

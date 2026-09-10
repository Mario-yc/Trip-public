type JsonRecord = Record<string, unknown>;

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
    if (left.optionId < right.optionId) return -1;
    if (left.optionId > right.optionId) return 1;
    return 0;
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
  const optionId = requireIdentity(option.id, `detour option ${index} identity`);
  const label = requireIdentity(option.label, `detour option ${index} label`);
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

function requireRecord(value: unknown, field: string): JsonRecord {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error(`${field} must be an object`);
  }
  return value as JsonRecord;
}

function requireIdentity(value: unknown, field: string): string {
  if (typeof value !== "string" || !value || value.trim() !== value) {
    throw new Error(`${field} must be a non-empty exact string`);
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
  const keys = Object.keys(value).sort();
  const allowed = new Set([...required, ...optional]);
  if (
    required.some((key) => !Object.prototype.hasOwnProperty.call(value, key)) ||
    keys.some((key) => !allowed.has(key))
  ) {
    throw new Error(`${field} contains missing or unknown fields`);
  }
}

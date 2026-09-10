type JsonRecord = Record<string, unknown>;

function asRecord(value: unknown): JsonRecord {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as JsonRecord)
    : {};
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

export type ClarificationSubmissionEvidence = {
  checkpointId: string;
  checkpointFingerprint: string;
  sourceAssistantTurnId: string;
  assistantTurnId: string;
  assistantStatus: string;
  executionStatus: string;
};

export type ClarificationSubmissionSource = {
  checkpointId: string;
  checkpointFingerprint: string;
  sourceAssistantTurnId: string;
};

export function assertLatestClarificationSubmissionSucceeded(
  sessionValue: unknown,
  sourceValue: ClarificationSubmissionSource,
): ClarificationSubmissionEvidence {
  const sourceAssistantTurnId = String(
    sourceValue?.sourceAssistantTurnId || "",
  );
  const checkpointId = String(sourceValue?.checkpointId || "");
  const checkpointFingerprint = String(
    sourceValue?.checkpointFingerprint || "",
  );
  if (!sourceAssistantTurnId || !checkpointId || !checkpointFingerprint) {
    throw new Error("clarification_submit_source_identity_missing");
  }

  const turns = asArray(asRecord(sessionValue).turns).map(asRecord);
  const sourceTurns = turns.filter(
    (turn) =>
      String(turn.id || "") === sourceAssistantTurnId &&
      String(turn.role || "") === "assistant",
  );
  if (sourceTurns.length !== 1) {
    throw new Error(
      `clarification_submit_source_identity_invalid:${JSON.stringify({
        sourceAssistantTurnId,
        sourceMatchCount: sourceTurns.length,
      })}`,
    );
  }

  const sourceCheckpoint = asRecord(sourceTurns[0].clarificationCheckpoint);
  if (
    String(sourceCheckpoint.checkpointId || "") !== checkpointId ||
    String(sourceCheckpoint.fingerprint || "") !== checkpointFingerprint ||
    String(sourceCheckpoint.sourceAssistantTurnId || "") !==
      sourceAssistantTurnId
  ) {
    throw new Error(
      `clarification_submit_checkpoint_identity_invalid:${JSON.stringify({
        sourceAssistantTurnId,
        checkpointId,
      })}`,
    );
  }

  const sourceTurnIndex = Number(sourceTurns[0].turnIndex);
  if (!Number.isInteger(sourceTurnIndex) || sourceTurnIndex < 0) {
    throw new Error(
      `clarification_submit_source_index_invalid:${JSON.stringify({
        sourceAssistantTurnId,
      })}`,
    );
  }

  const resultTurns = turns.filter(
    (turn) =>
      String(turn.role || "") === "assistant" &&
      Number(turn.turnIndex) > sourceTurnIndex,
  );
  if (resultTurns.length !== 1) {
    throw new Error(
      `clarification_submit_result_count_invalid:${JSON.stringify({
        sourceAssistantTurnId,
        resultTurnCount: resultTurns.length,
      })}`,
    );
  }

  const result = resultTurns[0];
  const trace = asRecord(result.structuredChoiceTrace);
  const assistantTurnId = String(result.id || "");
  if (String(trace.sourceAssistantTurnId || "") !== sourceAssistantTurnId) {
    throw new Error(
      `clarification_submit_lineage_invalid:${JSON.stringify({
        sourceAssistantTurnId,
        assistantTurnId,
        traceSourceAssistantTurnId: String(trace.sourceAssistantTurnId || ""),
      })}`,
    );
  }
  const traceCheckpointId = String(trace.checkpointId || "");
  const traceCheckpointFingerprint = String(trace.checkpointFingerprint || "");
  if (!traceCheckpointId || !traceCheckpointFingerprint) {
    throw new Error(
      `clarification_submit_checkpoint_identity_missing:${JSON.stringify({
        sourceAssistantTurnId,
        assistantTurnId,
      })}`,
    );
  }
  if (traceCheckpointId !== checkpointId) {
    throw new Error(
      `clarification_submit_checkpoint_id_invalid:${JSON.stringify({
        sourceAssistantTurnId,
        assistantTurnId,
      })}`,
    );
  }
  if (traceCheckpointFingerprint !== checkpointFingerprint) {
    throw new Error(
      `clarification_submit_checkpoint_fingerprint_invalid:${JSON.stringify({
        sourceAssistantTurnId,
        assistantTurnId,
      })}`,
    );
  }
  const evidence: ClarificationSubmissionEvidence = {
    checkpointId,
    checkpointFingerprint,
    sourceAssistantTurnId,
    assistantTurnId,
    assistantStatus: String(result.status || ""),
    executionStatus: String(trace.executionStatus || ""),
  };
  if (!evidence.assistantTurnId) {
    throw new Error(
      `clarification_submit_result_identity_missing:${JSON.stringify({
        sourceAssistantTurnId,
      })}`,
    );
  }
  if (
    evidence.assistantStatus !== "active" ||
    evidence.executionStatus !== "succeeded"
  ) {
    throw new Error(
      `clarification_submit_failed:${JSON.stringify({
        sourceAssistantTurnId: evidence.sourceAssistantTurnId,
        assistantTurnId: evidence.assistantTurnId,
        assistantStatus: evidence.assistantStatus,
        executionStatus: evidence.executionStatus,
      })}`,
    );
  }
  return evidence;
}

export function validateClarificationDimensionIdentitySet(
  actualValues: readonly unknown[],
  expectedValues: readonly string[],
): string[] {
  const actual = actualValues.map((value) => {
    if (typeof value !== "string" || value.trim() === "") {
      throw new Error(
        "clarification dimension identity must be a non-empty string",
      );
    }
    return value;
  });
  const expected = expectedValues.map((value) => {
    if (typeof value !== "string" || value.trim() === "") {
      throw new Error("expected clarification dimension must be non-empty");
    }
    return value;
  });

  const actualSet = new Set(actual);
  const expectedSet = new Set(expected);
  if (actualSet.size !== actual.length) {
    throw new Error("duplicate clarification dimension identity");
  }
  if (expectedSet.size !== expected.length) {
    throw new Error("duplicate expected clarification dimension identity");
  }

  const missing = expected.filter((value) => !actualSet.has(value));
  const unexpected = actual.filter((value) => !expectedSet.has(value));
  if (missing.length || unexpected.length) {
    throw new Error(
      `clarification dimension identity mismatch: missing=${missing.sort().join(",")}; unexpected=${unexpected.sort().join(",")}`,
    );
  }
  return [...actual].sort();
}

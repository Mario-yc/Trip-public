import { useEffect, useMemo, useRef, useState } from "react";
import type {
  AgentChoiceOption,
  ClarificationBatchSelection,
  ClarificationCheckpoint,
  ClarificationSubmission,
  MapPoi
} from "../../services/apiClient";
import { apiClient } from "../../services/apiClient";

type BatchDraft = Record<string, { optionId?: string; manualValue?: string }>;

export function AgentClarificationBatch({
  checkpoint,
  capabilityCurrent,
  disabled,
  executing,
  sessionId,
  selectedMapPoi,
  submission,
  submitOption,
  turnId,
  onSubmit
}: {
  checkpoint: ClarificationCheckpoint;
  capabilityCurrent: boolean;
  disabled: boolean;
  executing: boolean;
  sessionId: string;
  selectedMapPoi?: MapPoi | null;
  submission?: ClarificationSubmission | null;
  submitOption: AgentChoiceOption;
  turnId: string;
  onSubmit: (selections: ClarificationBatchSelection[]) => Promise<boolean>;
}) {
  const [boundCheckpoint, setBoundCheckpoint] = useState<ClarificationCheckpoint | null>(null);
  const [mapRadiusKm, setMapRadiusKm] = useState("");
  const effectiveCheckpoint = boundCheckpoint ?? checkpoint;
  const questions = useMemo(() => effectiveCheckpoint.questions ?? [], [effectiveCheckpoint.questions]);
  const includesSpatialQuestion = questions.some((question) => question.dimensionId === "spatial_focus");
  const storageKey = `trip.clarificationBatchDraft:${sessionId}:${checkpoint.checkpointId}:${checkpoint.fingerprint ?? ""}`;
  const [draft, setDraft] = useState<BatchDraft>({});
  const [submitError, setSubmitError] = useState("");
  const [submittedSummary, setSubmittedSummary] = useState<string[]>([]);
  const groupRefs = useRef<Record<string, HTMLFieldSetElement | null>>({});

  useEffect(() => {
    try {
      const storagePrefix = `trip.clarificationBatchDraft:${sessionId}:${checkpoint.checkpointId}:`;
      for (let index = window.sessionStorage.length - 1; index >= 0; index -= 1) {
        const key = window.sessionStorage.key(index);
        if (key?.startsWith(storagePrefix) && key !== storageKey) {
          window.sessionStorage.removeItem(key);
        }
      }
      const stored = window.sessionStorage.getItem(storageKey);
      setDraft(stored ? (JSON.parse(stored) as BatchDraft) : {});
      setSubmittedSummary([]);
      setBoundCheckpoint(null);
    } catch {
      setDraft({});
    }
  }, [checkpoint.checkpointId, sessionId, storageKey]);

  useEffect(() => {
    if (!capabilityCurrent) {
      window.sessionStorage.removeItem(storageKey);
    }
  }, [capabilityCurrent, storageKey]);

  useEffect(() => {
    if (!Object.keys(draft).length) return;
    window.sessionStorage.setItem(storageKey, JSON.stringify(draft));
  }, [draft, storageKey]);

  const unanswered = questions.filter((question) => {
    const answer = draft[question.dimensionId];
    return !answer?.optionId && !answer?.manualValue?.trim();
  });
  const isComplete = questions.length > 0 && unanswered.length === 0;

  function selectOption(dimensionId: string, optionId: string) {
    setSubmitError("");
    setDraft((current) => ({ ...current, [dimensionId]: { optionId } }));
  }

  function selectManual(dimensionId: string) {
    setSubmitError("");
    setDraft((current) => ({ ...current, [dimensionId]: { manualValue: current[dimensionId]?.manualValue ?? "" } }));
  }

  function updateManual(dimensionId: string, manualValue: string) {
    setSubmitError("");
    setDraft((current) => ({ ...current, [dimensionId]: { manualValue } }));
  }

  async function submit() {
    if (!isComplete || disabled || executing) {
      groupRefs.current[unanswered[0]?.dimensionId]?.focus();
      return;
    }
    const selections: ClarificationBatchSelection[] = questions.map((question) => ({
      dimensionId: question.dimensionId,
      ...(draft[question.dimensionId]?.optionId
        ? { optionId: draft[question.dimensionId].optionId }
        : { manualValue: draft[question.dimensionId]?.manualValue?.trim() })
    }));
    const succeeded = await onSubmit(selections);
    if (!succeeded) {
      setSubmitError("本批未提交，已保留你的选择。请检查提示后重试。");
      groupRefs.current[questions[0]?.dimensionId]?.focus();
      return;
    }
    const summary = questions.map((question) => {
      const answer = draft[question.dimensionId];
      return (
        question.options.find((option) => option.id === answer?.optionId)?.label ??
        answer?.manualValue?.trim() ??
        ""
      );
    });
    setSubmittedSummary(summary.filter(Boolean));
    window.sessionStorage.removeItem(storageKey);
  }

  async function bindCurrentMapSelection() {
    const radiusKm = Number(mapRadiusKm);
    const amapPoiId = String(selectedMapPoi?.amapId ?? selectedMapPoi?.id ?? "").trim();
    if (!amapPoiId || !Number.isFinite(radiusKm) || radiusKm < 0.1 || radiusKm > 50) {
      setSubmitError("请先在地图选择真实地点，并填写 0.1–50 公里的范围。");
      return;
    }
    try {
      const result = await apiClient.bindSpatialMapSelection(sessionId, {
        sourceAssistantTurnId: turnId,
        checkpointId: effectiveCheckpoint.checkpointId,
        checkpointFingerprint: effectiveCheckpoint.fingerprint ?? "",
        amapPoiId,
        label: selectedMapPoi?.name ?? "地图选点",
        radiusMeters: radiusKm * 1000
      });
      setBoundCheckpoint(result.checkpoint);
      selectOption("spatial_focus", result.optionId);
    } catch {
      setSubmitError("地图选点未绑定到当前问题，已保留其他选择。请刷新后重试。");
    }
  }

  if (submission?.status === "succeeded") {
    const answerByDimension = new Map(submission.answers.map((answer) => [answer.dimensionId, answer]));
    return (
      <section aria-label="已提交的关键决策" className="clarification-batch-card is-submitted">
        <strong>关键决策已确认</strong>
        <dl>
          {questions.map((question) => {
            const answer = answerByDimension.get(question.dimensionId);
            return answer ? (
              <div key={question.dimensionId}>
                <dt>{question.question}</dt>
                <dd>{answer.label}</dd>
              </div>
            ) : null;
          })}
        </dl>
      </section>
    );
  }

  if (submittedSummary.length > 0) {
    return (
      <section aria-label="已提交的关键决策" className="clarification-batch-card is-submitted">
        <strong>本批关键决策已提交</strong>
        <p>{submittedSummary.join(" · ")}</p>
        <p>后续仍可能继续核验活动区域等约束；全部约束可执行后，才会检索真实地点与路线。</p>
      </section>
    );
  }

  return (
    <section
      aria-busy={executing}
      aria-label="批量澄清"
      className="clarification-batch-card"
      data-checkpoint-id={checkpoint.checkpointId}
      data-source-turn-id={turnId}
    >
      <header>
        <strong>还需确认 {questions.length} 项关键决策</strong>
        <p>已从原始需求提取 {(checkpoint.experienceSpecs ?? []).length} 项体验偏好。</p>
        <p>
          本轮需确认 {questions.length} 项决策，已选择 {questions.length - unanswered.length}/{questions.length}
          {includesSpatialQuestion
            ? "；提交后继续核验活动区域，范围可执行后才检索真实地点与路线。"
            : "；确认后开始检索真实地点与路线。"}
        </p>
      </header>
      {questions.map((question) => {
        const answer = draft[question.dimensionId] ?? {};
        const manualSelected = Object.prototype.hasOwnProperty.call(answer, "manualValue");
        return (
          <fieldset
            key={question.dimensionId}
            ref={(node) => {
              groupRefs.current[question.dimensionId] = node;
            }}
            tabIndex={-1}
          >
            <legend>{question.question}</legend>
            <p className="clarification-batch-why">为什么需要确认：{question.whyItMatters}</p>
            {question.options.map((option) => (
              <label key={option.id}>
                <input
                  checked={answer.optionId === option.id}
                  data-option-id={option.id}
                  disabled={disabled || executing}
                  name={`${checkpoint.checkpointId}:${question.dimensionId}`}
                  onChange={() => selectOption(question.dimensionId, option.id)}
                  type="radio"
                />
                <span>{option.label}</span>
              </label>
            ))}
            {question.allowFreeText ? (
              <label className="clarification-batch-manual">
                <span>
                  <input
                    checked={manualSelected}
                    disabled={disabled || executing}
                    name={`${checkpoint.checkpointId}:${question.dimensionId}`}
                    onChange={() => selectManual(question.dimensionId)}
                    type="radio"
                  />
                  其他，我来补充
                </span>
                <input
                  aria-label={`${question.question}的补充内容`}
                  disabled={disabled || executing}
                  maxLength={800}
                  onClick={() => selectManual(question.dimensionId)}
                  onFocus={() => selectManual(question.dimensionId)}
                  onChange={(event) => updateManual(question.dimensionId, event.target.value)}
                  placeholder="输入只属于本问题的偏好或限制"
                  type="text"
                  value={answer.manualValue ?? ""}
                />
              </label>
            ) : null}
            {question.dimensionId === "spatial_focus" ? (
              <div className="clarification-batch-map-selection">
                <p>{selectedMapPoi ? `当前地图选点：${selectedMapPoi.name}` : "请先在地图选择一个真实地点。"}</p>
                <label>
                  选点范围（公里）
                  <input
                    aria-label="地图选点范围（公里）"
                    disabled={disabled || executing}
                    inputMode="decimal"
                    max="50"
                    min="0.1"
                    onChange={(event) => setMapRadiusKm(event.target.value)}
                    placeholder="例如 3"
                    step="0.1"
                    type="number"
                    value={mapRadiusKm}
                  />
                </label>
                <button
                  disabled={disabled || executing || !selectedMapPoi || !mapRadiusKm}
                  onClick={() => void bindCurrentMapSelection()}
                  type="button"
                >
                  使用当前地图选点
                </button>
              </div>
            ) : null}
          </fieldset>
        );
      })}
      <p aria-live="polite" className="clarification-batch-status">
        {submitError ||
          (unanswered.length
            ? `仍未回答：${unanswered.map((question) => question.question).join("；")}`
            : includesSpatialQuestion
              ? "所有关键决策已选择；提交后将继续核验活动区域。"
              : "所有关键决策已选择，可以开始规划。")}
      </p>
      <button
        className="clarification-batch-submit"
        data-choice-action={submitOption.action}
        data-choice-id={submitOption.id}
        disabled={!isComplete || disabled || executing}
        onClick={() => void submit()}
        type="button"
      >
        {includesSpatialQuestion
          ? executing
            ? "正在提交并核验活动区域…"
            : submitOption.label || "提交并继续核验活动区域"
          : executing
            ? "正在确认并开始规划…"
            : submitOption.label || "确认并开始规划"}
      </button>
    </section>
  );
}

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";
import { AgentClarificationBatch } from "../../src/components/agent/AgentClarificationBatch";
import { apiClient, type ClarificationCheckpoint } from "../../src/services/apiClient";

function checkpoint(fingerprint = "fingerprint-a", detourAllowFreeText = false): ClarificationCheckpoint {
  return {
    schemaVersion: "clarification-checkpoint-v2",
    checkpointId: "checkpoint-batch",
    planningRootId: "turn-root",
    fingerprint,
    submissionMode: "batch_atomic",
    submitChoiceId: "clarification-batch:checkpoint-batch",
    status: "awaiting_answer",
    experienceSpecs: [{ kind: "campus" }],
    questions: [
      {
        dimensionId: "route_decision.mobility_profile",
        question: "希望采用哪种主要交通方式？",
        whyItMatters: "会改变真实路线比较。",
        required: true,
        allowFreeText: false,
        options: [
          { id: "transit", label: "公共交通为主", semanticValue: { mobilityProfile: { transportMode: "transit" } } },
          { id: "walking", label: "步行为主", semanticValue: { mobilityProfile: { transportMode: "walking" } } }
        ]
      },
      {
        dimensionId: "route_decision.detour_tolerance",
        question: "更看重少绕路还是体验变化？",
        whyItMatters: "会约束候选路线的可接受偏绕。",
        required: true,
        allowFreeText: detourAllowFreeText,
        options: [
          {
            id: "less_detour",
            label: "尽量少绕路",
            semanticValue: {
              detourTolerance: { maxGeneralizedCostDelta: 15, maxDetourRatio: 0.15 }
            }
          },
          {
            id: "more_variety",
            label: "接受少量绕路",
            semanticValue: {
              detourTolerance: { maxGeneralizedCostDelta: 30, maxDetourRatio: 0.3 }
            }
          }
        ]
      }
    ]
  };
}

const submitOption = {
  id: "clarification-batch:checkpoint-batch",
  action: "submit_clarification_batch" as const,
  kind: "clarification_batch_submit",
  scopeKind: "clarification" as const,
  label: "确认并开始规划"
};

afterEach(() => {
  vi.restoreAllMocks();
  cleanup();
  window.sessionStorage.clear();
});

describe("AgentClarificationBatch", () => {
  test("renders persisted successful answers as read-only history after remount", () => {
    const props = {
      capabilityCurrent: false,
      checkpoint: checkpoint(),
      disabled: true,
      executing: false,
      onSubmit: vi.fn(async () => true),
      sessionId: "sess-batch",
      submission: {
        checkpointId: "checkpoint-batch",
        sourceAssistantTurnId: "turn-assistant",
        requestUserTurnId: "turn-user-answer",
        executionId: "exec-1",
        status: "succeeded" as const,
        answers: [
          {
            dimensionId: "route_decision.mobility_profile",
            optionId: "transit",
            label: "公共交通为主",
            source: "opaque_option"
          },
          {
            dimensionId: "route_decision.detour_tolerance",
            optionId: "less_detour",
            label: "尽量少绕路",
            source: "opaque_option"
          }
        ]
      },
      submitOption,
      turnId: "turn-assistant"
    };
    const first = render(<AgentClarificationBatch {...props} />);
    expect(screen.getByLabelText("已提交的关键决策").textContent).toContain("公共交通为主");
    expect(screen.getByLabelText("已提交的关键决策").textContent).toContain("尽量少绕路");
    expect(screen.queryAllByRole("radio")).toHaveLength(0);
    expect(screen.queryByRole("button", { name: "确认并开始规划" })).toBeNull();
    first.unmount();

    render(<AgentClarificationBatch {...props} />);
    expect(screen.getByText("公共交通为主")).toBeTruthy();
    expect(screen.getByText("尽量少绕路")).toBeTruthy();
    expect(screen.queryAllByRole("radio")).toHaveLength(0);
  });

  test("groups all questions and submits one opaque batch only after every answer is present", async () => {
    const onSubmit = vi.fn(async () => true);
    render(
      <AgentClarificationBatch
        capabilityCurrent
        checkpoint={checkpoint()}
        disabled={false}
        executing={false}
        onSubmit={onSubmit}
        sessionId="sess-batch"
        submitOption={submitOption}
        turnId="turn-assistant"
      />
    );

    expect(screen.getAllByRole("group")).toHaveLength(2);
    const submit = screen.getByRole("button", { name: "确认并开始规划" });
    expect((submit as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("radio", { name: "公共交通为主" }));
    expect((submit as HTMLButtonElement).disabled).toBe(true);
    const detourGroup = screen.getAllByRole("group")[1];
    expect(within(detourGroup).queryByRole("textbox")).toBeNull();
    expect(within(detourGroup).queryByRole("radio", { name: "其他，我来补充" })).toBeNull();
    const strictDetourRadio = screen.getByRole("radio", { name: "尽量少绕路" });
    expect(strictDetourRadio.getAttribute("data-option-id")).toBe("less_detour");
    fireEvent.click(strictDetourRadio);
    expect((submit as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(submit);

    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
    expect(onSubmit).toHaveBeenCalledWith([
      { dimensionId: "route_decision.mobility_profile", optionId: "transit" },
      { dimensionId: "route_decision.detour_tolerance", optionId: "less_detour" }
    ]);
    const submitted = await screen.findByLabelText("已提交的关键决策");
    expect(submitted.textContent).toContain("本批关键决策已提交");
    expect(submitted.textContent).toContain("公共交通为主 · 尽量少绕路");
    expect(submitted.textContent).toContain(
      "后续仍可能继续核验活动区域等约束；全部约束可执行后，才会检索真实地点与路线。"
    );
    expect(submitted.textContent).not.toContain("已确认并开始规划");
    expect(
      window.sessionStorage.getItem("trip.clarificationBatchDraft:sess-batch:checkpoint-batch:fingerprint-a")
    ).toBeNull();
  });

  test("restores a draft for the same fingerprint and clears it when the fingerprint changes", () => {
    const first = render(
      <AgentClarificationBatch
        capabilityCurrent
        checkpoint={checkpoint()}
        disabled={false}
        executing={false}
        onSubmit={async () => false}
        sessionId="sess-batch"
        submitOption={submitOption}
        turnId="turn-assistant"
      />
    );
    fireEvent.click(screen.getByRole("radio", { name: "公共交通为主" }));
    first.unmount();

    const restored = render(
      <AgentClarificationBatch
        capabilityCurrent
        checkpoint={checkpoint()}
        disabled={false}
        executing={false}
        onSubmit={async () => false}
        sessionId="sess-batch"
        submitOption={submitOption}
        turnId="turn-assistant"
      />
    );
    expect((screen.getByRole("radio", { name: "公共交通为主" }) as HTMLInputElement).checked).toBe(true);
    restored.unmount();

    render(
      <AgentClarificationBatch
        capabilityCurrent
        checkpoint={checkpoint("fingerprint-b")}
        disabled={false}
        executing={false}
        onSubmit={async () => false}
        sessionId="sess-batch"
        submitOption={submitOption}
        turnId="turn-assistant"
      />
    );
    expect((screen.getByRole("radio", { name: "公共交通为主" }) as HTMLInputElement).checked).toBe(false);
    expect(
      window.sessionStorage.getItem("trip.clarificationBatchDraft:sess-batch:checkpoint-batch:fingerprint-a")
    ).toBeNull();
  });

  test("keeps selections after a failed submit and moves focus to the first question", async () => {
    render(
      <AgentClarificationBatch
        capabilityCurrent
        checkpoint={checkpoint("fingerprint-a", true)}
        disabled={false}
        executing={false}
        onSubmit={async () => false}
        sessionId="sess-batch"
        submitOption={submitOption}
        turnId="turn-assistant"
      />
    );
    fireEvent.click(screen.getByRole("radio", { name: "公共交通为主" }));
    const secondGroup = screen.getAllByRole("group")[1];
    const manualInput = within(secondGroup).getByRole("textbox");
    fireEvent.click(manualInput);
    expect((within(secondGroup).getByRole("radio", { name: "其他，我来补充" }) as HTMLInputElement).checked).toBe(true);
    fireEvent.change(manualInput, { target: { value: "最多步行二十分钟" } });
    fireEvent.click(screen.getByRole("button", { name: "确认并开始规划" }));

    expect(await screen.findByText("本批未提交，已保留你的选择。请检查提示后重试。")).toBeTruthy();
    expect((screen.getByRole("radio", { name: "公共交通为主" }) as HTMLInputElement).checked).toBe(true);
    expect(document.activeElement).toBe(screen.getAllByRole("group")[0]);
  });

  test("binds a current AMap marker and explicit range before submitting the opaque map option", async () => {
    const spatialCheckpoint: ClarificationCheckpoint = {
      ...checkpoint(),
      questions: [
        {
          dimensionId: "spatial_focus",
          question: "活动区域希望限定在哪里？",
          whyItMatters: "用于约束候选范围。",
          required: true,
          allowFreeText: true,
          options: []
        }
      ]
    };
    const boundCheckpoint: ClarificationCheckpoint = {
      ...spatialCheckpoint,
      fingerprint: "fingerprint-bound-map",
      questions: [
        {
          ...spatialCheckpoint.questions![0],
          options: [
            {
              id: "spatial_map_bound",
              label: "地图选点：清华大学 · 3km",
              semanticValue: {
                spatialResolutionInput: { kind: "map_selection", mapSelectionFingerprint: "a".repeat(64) }
              }
            }
          ]
        }
      ]
    };
    const bind = vi.spyOn(apiClient, "bindSpatialMapSelection").mockResolvedValue({
      optionId: "spatial_map_bound",
      label: "地图选点：清华大学 · 3km",
      mapSelectionFingerprint: "a".repeat(64),
      checkpoint: boundCheckpoint
    });
    const onSubmit = vi.fn(async () => true);
    render(
      <AgentClarificationBatch
        capabilityCurrent
        checkpoint={spatialCheckpoint}
        disabled={false}
        executing={false}
        onSubmit={onSubmit}
        selectedMapPoi={{
          id: "B000A6EA36",
          amapId: "B000A6EA36",
          name: "清华大学",
          type: "高等院校",
          city: "北京",
          district: "海淀区",
          address: "双清路30号",
          longitude: 116.326,
          latitude: 40.003,
          category: "campus",
          source: "amap-place-search",
          sourceNote: "AMap",
          confidence: 1,
          photos: []
        }}
        sessionId="sess-batch"
        submitOption={submitOption}
        turnId="turn-assistant"
      />
    );
    fireEvent.change(screen.getByRole("spinbutton", { name: "地图选点范围（公里）" }), {
      target: { value: "3" }
    });
    fireEvent.click(screen.getByRole("button", { name: "使用当前地图选点" }));
    await waitFor(() => expect(bind).toHaveBeenCalledTimes(1));
    expect(bind).toHaveBeenCalledWith("sess-batch", {
      sourceAssistantTurnId: "turn-assistant",
      checkpointId: "checkpoint-batch",
      checkpointFingerprint: "fingerprint-a",
      amapPoiId: "B000A6EA36",
      label: "清华大学",
      radiusMeters: 3000
    });
    expect(((await screen.findByRole("radio", { name: "地图选点：清华大学 · 3km" })) as HTMLInputElement).checked).toBe(
      true
    );
    expect(screen.getByText("所有关键决策已选择；提交后将继续核验活动区域。")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: submitOption.label }));
    await waitFor(() =>
      expect(onSubmit).toHaveBeenCalledWith([{ dimensionId: "spatial_focus", optionId: "spatial_map_bound" }])
    );
  });
});

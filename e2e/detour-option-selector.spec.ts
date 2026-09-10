import { expect, test } from "@playwright/test";

import { selectStrictDetourOption } from "./support/detour-option-selector";

type OptionSeed = {
  id?: string;
  ratio: number;
  delta: number;
  semanticExtra?: Record<string, unknown>;
  toleranceExtra?: Record<string, unknown>;
  adjacentLegConstraint?: Record<string, unknown>;
};

function question(options: OptionSeed[], allowFreeText = false) {
  return {
    dimensionId: "route_decision.detour_tolerance",
    question: "您对绕行和额外行程时间的接受程度如何？",
    allowFreeText,
    options: options.map((option, index) => ({
      id: option.id,
      label: `服务端候选 ${index + 1}`,
      semanticValue: {
        detourTolerance: {
          maxGeneralizedCostDelta: option.delta,
          maxDetourRatio: option.ratio,
          ...option.toleranceExtra,
        },
        ...(option.adjacentLegConstraint
          ? { adjacentLegConstraint: option.adjacentLegConstraint }
          : {}),
        ...option.semanticExtra,
      },
    })),
  };
}

test("selects the legacy strict option without binding to its numeric values", () => {
  const selected = selectStrictDetourOption(
    question([
      { id: "legacy-strict", ratio: 0.15, delta: 15 },
      { id: "legacy-moderate", ratio: 0.3, delta: 30 },
    ]),
  );

  expect(selected.optionId).toBe("legacy-strict");
});

test("selects the current server-issued strict option", () => {
  const selected = selectStrictDetourOption(
    question([
      { id: "strict", ratio: 0.2, delta: 20 },
      { id: "moderate", ratio: 0.35, delta: 35 },
      { id: "flexible", ratio: 0.5, delta: 50 },
    ]),
  );

  expect(selected).toMatchObject({
    optionId: "strict",
    submissionMode: "persisted_option",
    semanticValue: {
      detourTolerance: {
        maxGeneralizedCostDelta: 20,
        maxDetourRatio: 0.2,
      },
    },
  });
});

test("is stable under server option reordering", () => {
  const first = selectStrictDetourOption(
    question([
      { id: "moderate", ratio: 0.35, delta: 35 },
      { id: "strict", ratio: 0.2, delta: 20 },
      { id: "flexible", ratio: 0.5, delta: 50 },
    ]),
  );
  const reordered = selectStrictDetourOption(
    question([
      { id: "flexible", ratio: 0.5, delta: 50 },
      { id: "moderate", ratio: 0.35, delta: 35 },
      { id: "strict", ratio: 0.2, delta: 20 },
    ]),
  );

  expect(first.optionId).toBe("strict");
  expect(reordered.optionId).toBe(first.optionId);
});

test("continues to select the strict option when legal numbers change", () => {
  const selected = selectStrictDetourOption(
    question([
      { id: "server-z", ratio: 0.18, delta: 27 },
      { id: "server-a", ratio: 0.12, delta: 41 },
      { id: "server-m", ratio: 0.12, delta: 33 },
    ]),
  );

  expect(selected.optionId).toBe("server-m");
});

test("uses stable option identity only after ratio and delta ties", () => {
  const selected = selectStrictDetourOption(
    question([
      { id: "server-z", ratio: 0.2, delta: 20 },
      { id: "server-a", ratio: 0.2, delta: 20 },
    ]),
  );

  expect(selected.optionId).toBe("server-a");
});

test("accepts the exact optional adjacent-leg schema", () => {
  const selected = selectStrictDetourOption(
    question([
      {
        id: "strict",
        ratio: 0.2,
        delta: 20,
        adjacentLegConstraint: {
          candidateSearchRadiusMeters: 5000,
          maxProviderTravelMinutes: 45,
        },
      },
      { id: "moderate", ratio: 0.35, delta: 35 },
    ]),
  );

  expect(selected.semanticValue.adjacentLegConstraint).toEqual({
    candidateSearchRadiusMeters: 5000,
    maxProviderTravelMinutes: 45,
  });
});

const invalidCases = [
  ["free text enabled", question([
    { id: "strict", ratio: 0.2, delta: 20 },
    { id: "moderate", ratio: 0.35, delta: 35 },
  ], true)],
  ["missing option identity", question([
    { ratio: 0.2, delta: 20 },
    { id: "moderate", ratio: 0.35, delta: 35 },
  ])],
  ["non-finite delta", question([
    { id: "strict", ratio: 0.2, delta: Number.POSITIVE_INFINITY },
    { id: "moderate", ratio: 0.35, delta: 35 },
  ])],
  ["non-positive delta", question([
    { id: "strict", ratio: 0.2, delta: 0 },
    { id: "moderate", ratio: 0.35, delta: 35 },
  ])],
  ["ratio below range", question([
    { id: "strict", ratio: -0.01, delta: 20 },
    { id: "moderate", ratio: 0.35, delta: 35 },
  ])],
  ["ratio above range", question([
    { id: "strict", ratio: 1.01, delta: 20 },
    { id: "moderate", ratio: 0.35, delta: 35 },
  ])],
  ["unknown semantic field", question([
    { id: "strict", ratio: 0.2, delta: 20, semanticExtra: { unknown: true } },
    { id: "moderate", ratio: 0.35, delta: 35 },
  ])],
  ["unknown tolerance field", question([
    { id: "strict", ratio: 0.2, delta: 20, toleranceExtra: { unknown: true } },
    { id: "moderate", ratio: 0.35, delta: 35 },
  ])],
  ["unknown adjacent-leg field", question([
    {
      id: "strict",
      ratio: 0.2,
      delta: 20,
      adjacentLegConstraint: {
        candidateSearchRadiusMeters: 5000,
        maxProviderTravelMinutes: 45,
        unknown: true,
      },
    },
    { id: "moderate", ratio: 0.35, delta: 35 },
  ])],
  ["duplicate option identity", question([
    { id: "duplicate", ratio: 0.2, delta: 20 },
    { id: "duplicate", ratio: 0.35, delta: 35 },
  ])],
  ["too few options", question([
    { id: "strict", ratio: 0.2, delta: 20 },
  ])],
] as const;

for (const [name, invalidQuestion] of invalidCases) {
  test(`fails closed for ${name}`, () => {
    expect(() => selectStrictDetourOption(invalidQuestion)).toThrow();
  });
}

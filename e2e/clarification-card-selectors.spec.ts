import { expect, test } from "@playwright/test";
import { readFile } from "node:fs/promises";
import path from "node:path";

test("clarification card selectors distinguish awaiting input from submitted history", async () => {
  const [simpleDirectionSource, frontierSource, twoDaySource, selectorSource] =
    await Promise.all([
      readFile(
        path.resolve("e2e", "simple-direction-user-journey.spec.ts"),
        "utf8",
      ),
      readFile(
        path.resolve("e2e", "simple-direction-frontier-user-journey.spec.ts"),
        "utf8",
      ),
      readFile(
        path.resolve("e2e", "two-day-meal-autonomy-user-journey.spec.ts"),
        "utf8",
      ),
      readFile(
        path.resolve("e2e", "support", "clarification-card-selectors.ts"),
        "utf8",
      ),
    ]);

  expect(selectorSource).toContain(
    'AWAITING_CLARIFICATION_BATCH_LABEL = "批量澄清"',
  );
  expect(selectorSource).toContain(
    'SUBMITTED_CLARIFICATION_HISTORY_LABEL = "已提交的关键决策"',
  );
  expect(simpleDirectionSource).toContain(
    "const historicalCards = submittedClarificationHistoryCards(page)",
  );
  expect(simpleDirectionSource).toContain(
    "awaitingClarificationBatchCards(page)",
  );
  expect(frontierSource).toContain("awaitingClarificationBatchCards(page)");
  expect(twoDaySource).toContain("awaitingClarificationBatchCards(page)");
  expect(frontierSource).not.toContain(
    "submittedClarificationHistoryCards(page)",
  );
  expect(twoDaySource).not.toContain(
    "submittedClarificationHistoryCards(page)",
  );
});

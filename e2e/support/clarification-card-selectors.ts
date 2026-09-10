import type { Locator, Page } from "@playwright/test";

export const AWAITING_CLARIFICATION_BATCH_LABEL = "批量澄清";
export const SUBMITTED_CLARIFICATION_HISTORY_LABEL = "已提交的关键决策";

export function awaitingClarificationBatchCards(page: Page): Locator {
  return page.getByRole("region", {
    name: AWAITING_CLARIFICATION_BATCH_LABEL,
    exact: true,
  });
}

export function submittedClarificationHistoryCards(page: Page): Locator {
  return page.getByRole("region", {
    name: SUBMITTED_CLARIFICATION_HISTORY_LABEL,
    exact: true,
  });
}

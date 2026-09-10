import { afterEach, expect, test } from "vitest";
import { plannerStore } from "../../src/state/plannerStore";
import { isCurrentVersionedWrite } from "../../src/state/versionedWriteGuard";

afterEach(() => {
  plannerStore.setState({ activeVersionId: null });
});

test("versioned write guard accepts writes without a base version", () => {
  plannerStore.setState({ activeVersionId: "ver_current" });

  expect(isCurrentVersionedWrite(null)).toBe(true);
  expect(isCurrentVersionedWrite(undefined)).toBe(true);
});

test("versioned write guard accepts only the current active version", () => {
  plannerStore.setState({ activeVersionId: "ver_current" });

  expect(isCurrentVersionedWrite("ver_current")).toBe(true);
  expect(isCurrentVersionedWrite("ver_old")).toBe(false);
});

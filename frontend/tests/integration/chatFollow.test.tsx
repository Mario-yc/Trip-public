import { act, renderHook } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { preferredScrollBehavior, useChatFollow } from "../../src/components/workspace/useChatFollow";

afterEach(() => vi.unstubAllGlobals());

test("new stream content follows only while the reader stays near the bottom", () => {
  const { result, rerender } = renderHook(({ update }) => useChatFollow(update, "session-a"), { initialProps: { update: "first" } });
  const node = document.createElement("section");
  Object.defineProperties(node, { clientHeight: { value: 200, configurable: true }, scrollHeight: { value: 1000, configurable: true } });
  result.current.ref.current = node;
  rerender({ update: "second" });
  expect(node.scrollTop).toBe(1000);
  node.scrollTop = 300;
  act(() => result.current.onScroll());
  Object.defineProperty(node, "scrollHeight", { value: 1400 });
  rerender({ update: "third" });
  expect(node.scrollTop).toBe(300);
  expect(result.current.showLatest).toBe(true);
  Object.defineProperty(node, "clientHeight", { value: 0 });
  act(() => result.current.onScroll());
  rerender({ update: "hidden-pane" });
  expect(node.scrollTop).toBe(300);
  Object.defineProperty(node, "clientHeight", { value: 200 });
  rerender({ update: "visible-pane" });
  expect(node.scrollTop).toBe(300);
  act(() => result.current.scrollLatest());
  expect(node.scrollTop).toBe(1400);
  expect(result.current.showLatest).toBe(false);
});

test("explicit scroll obeys reduced motion for both preferences", () => {
  vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: true })));
  expect(preferredScrollBehavior()).toBe("auto");
  vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: false })));
  expect(preferredScrollBehavior()).toBe("smooth");
});

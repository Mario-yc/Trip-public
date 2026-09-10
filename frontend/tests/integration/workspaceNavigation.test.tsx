import { fireEvent, render, screen } from "@testing-library/react";
import { expect, test } from "vitest";
import { WorkspacePanelSwitcher } from "../../src/components/workspace/WorkspacePanelSwitcher";

test("workspace panel switcher keeps a single selected tab and supports arrow navigation", () => {
  let activePanel: "agent" | "map" | "timeline" = "agent";
  const onChange = (panel: typeof activePanel) => {
    activePanel = panel;
    view.rerender(<WorkspacePanelSwitcher activePanel={activePanel} onChange={onChange} />);
  };
  const view = render(<WorkspacePanelSwitcher activePanel={activePanel} onChange={onChange} />);

  const agent = screen.getByRole("tab", { name: "Agent" });
  const map = screen.getByRole("tab", { name: "地图" });
  const timeline = screen.getByRole("tab", { name: "行程" });

  expect(agent.getAttribute("aria-selected")).toBe("true");
  expect(map.getAttribute("aria-selected")).toBe("false");
  expect(timeline.getAttribute("aria-controls")).toBe("workspace-pane-timeline");

  fireEvent.keyDown(agent, { key: "ArrowRight" });
  expect(map.getAttribute("aria-selected")).toBe("true");
  fireEvent.keyDown(map, { key: "ArrowRight" });
  expect(timeline.getAttribute("aria-selected")).toBe("true");
  fireEvent.click(agent);
  expect(agent.getAttribute("aria-selected")).toBe("true");
});

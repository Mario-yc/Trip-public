import type { KeyboardEvent } from "react";

type WorkspacePanel = "agent" | "map" | "timeline";

type WorkspacePanelSwitcherProps = {
  activePanel: WorkspacePanel;
  onChange: (panel: WorkspacePanel) => void;
};

const PANELS: Array<{ id: WorkspacePanel; label: string }> = [
  { id: "agent", label: "Agent" },
  { id: "map", label: "地图" },
  { id: "timeline", label: "行程" }
];

export function WorkspacePanelSwitcher({ activePanel, onChange }: WorkspacePanelSwitcherProps) {
  function handleKeyDown(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    const nextIndex = (index + (event.key === "ArrowRight" ? 1 : -1) + PANELS.length) % PANELS.length;
    const nextPanel = PANELS[nextIndex];
    onChange(nextPanel.id);
    document.getElementById(`workspace-panel-tab-${nextPanel.id}`)?.focus();
  }

  return (
    <nav aria-label="工作区面板" className="workspace-panel-switcher">
      <div aria-label="工作区面板" role="tablist">
        {PANELS.map((panel, index) => (
          <button
            aria-controls={`workspace-pane-${panel.id}`}
            aria-selected={activePanel === panel.id}
            id={`workspace-panel-tab-${panel.id}`}
            key={panel.id}
            onClick={() => onChange(panel.id)}
            onKeyDown={(event) => handleKeyDown(event, index)}
            role="tab"
            tabIndex={activePanel === panel.id ? 0 : -1}
            type="button"
          >
            {panel.label}
          </button>
        ))}
      </div>
    </nav>
  );
}

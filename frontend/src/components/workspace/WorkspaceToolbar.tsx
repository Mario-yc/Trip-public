import { type ReactNode, useEffect, useRef, useState } from "react";

/** A disclosure for existing controls; never owns a business action or remounts children. */
export function WorkspaceToolbar({ children }: { children: ReactNode }) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return;
    function dismiss(event: globalThis.KeyboardEvent) {
      if (event.key !== "Escape") return;
      setOpen(false);
      trigger.current?.focus();
    }
    function outside(event: globalThis.PointerEvent) {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    }
    window.addEventListener("keydown", dismiss);
    window.addEventListener("pointerdown", outside);
    return () => {
      window.removeEventListener("keydown", dismiss);
      window.removeEventListener("pointerdown", outside);
    };
  }, [open]);

  return (
    <div className={`workspace-toolbar ${open ? "is-open" : ""}`} ref={root}>
      <button aria-controls="workspace-tools" aria-expanded={open} aria-label="工作区操作" className="workspace-tools-trigger"
        onClick={() => setOpen((value) => !value)} ref={trigger} type="button">操作</button>
      <div className="workspace-tools" id="workspace-tools">{children}</div>
    </div>
  );
}

import { type MouseEventHandler, type ReactNode, useEffect, useRef } from "react";

/** Modal presentation wrapper; the supplied close callback remains authoritative. */
export function FocusDialog({ children, className, label, onClose, onMouseDown }: {
  children: ReactNode; className: string; label: string; onClose: () => void; onMouseDown?: MouseEventHandler<HTMLDivElement>;
}) {
  const root = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    root.current?.querySelector<HTMLButtonElement>("button")?.focus();
    return () => { if (opener?.isConnected && opener.getClientRects().length) opener.focus(); };
  }, []);
  return <div aria-label={label} aria-modal="true" className={className} ref={root} role="dialog" onMouseDown={onMouseDown}
    onKeyDown={(event) => {
      if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); onClose(); }
      if (event.key !== "Tab") return;
      const items = Array.from(root.current?.querySelectorAll<HTMLElement>('button:not(:disabled), a[href], input:not(:disabled), [tabindex="0"]') ?? []).filter((node) => node.getClientRects().length);
      const first = items[0]; const last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }}>{children}</div>;
}

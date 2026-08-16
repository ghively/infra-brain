import { useEffect, useRef } from "react";

/** Reusable slide-in detail drawer (DR-6.1 design foundation).
 *
 * Ported from the legacy DC-shell drawer pattern (dashboard/src/shell.dc.html —
 * `@keyframes drawerIn` / `@keyframes fadeIn` + overlay) that ~10 legacy pages
 * use for row-click → detail. The React port dropped this pattern entirely; this
 * component reinstates it as a single reusable primitive.
 *
 * Usage:
 *   const [selected, setSelected] = useState<MyRow | null>(null);
 *   ...
 *   <DetailDrawer title="Event detail" open={selected !== null} onClose={() => setSelected(null)}>
 *     {selected && <MyDetailContent item={selected} />}
 *   </DetailDrawer>
 *
 * Animation: `drawerIn` (translateX 100%→0) + `fadeIn` overlay, both defined
 * in index.css (design-foundation MR). CSS classes: `.ib-drawer-overlay`,
 * `.ib-drawer-panel`, `.ib-drawer-header`, `.ib-drawer-title`,
 * `.ib-drawer-close`, `.ib-drawer-body`.
 *
 * Accessibility: Escape closes; focus is returned to `triggerRef` on close
 * (pass the ref via `triggerRef` prop — optional; fallback is document.body).
 * The panel receives `role="dialog"` + `aria-modal="true"`.
 */
export interface DetailDrawerProps {
  open: boolean;
  onClose: () => void;
  title?: string;
  /** Optional ref to the element that triggered the open — focus returns here
   *  when the drawer closes, satisfying the WCAG 2.1 SC 2.4.3 focus sequence. */
  triggerRef?: React.RefObject<HTMLElement | null>;
  children?: React.ReactNode;
}

export function DetailDrawer({ open, onClose, title, triggerRef, children }: DetailDrawerProps) {
  const panelRef = useRef<HTMLDivElement>(null);

  // Escape key closes
  useEffect(() => {
    if (!open) return;
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  // Focus trap: move focus into panel on open; restore on close
  useEffect(() => {
    if (open) {
      // Small delay lets animation start before focus shifts
      const t = setTimeout(() => {
        const first = panelRef.current?.querySelector<HTMLElement>(
          "button, [href], input, select, textarea, [tabindex]:not([tabindex='-1'])"
        );
        (first ?? panelRef.current)?.focus();
      }, 50);
      return () => clearTimeout(t);
    } else {
      // Return focus to trigger or body
      const restore = triggerRef?.current ?? null;
      restore?.focus();
    }
  }, [open, triggerRef]);

  if (!open) return null;

  return (
    <>
      {/* Dimmed overlay — click outside closes */}
      <div className="ib-drawer-overlay" onClick={onClose} aria-hidden="true" />

      {/* Slide-in panel */}
      <div
        ref={panelRef}
        className="ib-drawer-panel"
        role="dialog"
        aria-modal="true"
        aria-label={title ?? "Detail"}
        tabIndex={-1}
      >
        <div className="ib-drawer-header">
          <span className="ib-drawer-title">{title ?? "Detail"}</span>
          <button className="ib-drawer-close" onClick={onClose} aria-label="Close drawer">
            ×
          </button>
        </div>
        <div className="ib-drawer-body">{children}</div>
      </div>
    </>
  );
}

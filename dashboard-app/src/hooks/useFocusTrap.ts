import { useEffect, useRef } from "react";

const FOCUSABLE_SELECTOR =
  'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

export type FocusTrapOptions = {
  /** Element to return focus to on close. Pass this explicitly rather than
   * relying on document.activeElement when the trigger element (e.g. a
   * toggle button) is conditionally unmounted in the same render pass that
   * mounts the trap container — the DOM spec moves focus to <body> the
   * instant a focused node is removed, which happens during React's commit
   * phase, before this hook's effect ever runs. Without this, "return
   * focus" would silently restore focus to <body> instead of the button. */
  restoreFocusRef?: React.RefObject<HTMLElement | null>;
  /** Called when Escape is pressed while the trap is active (e.g. to close
   * the dialog/drawer). Optional — omit for a trap with no Escape affordance. */
  onEscape?: () => void;
};

/** Focus trap + return-focus for a dialog-like panel (drawer, modal).
 *
 * MR-I a11y fix: the FRONTEND_UX_AUDIT_2026-07-06 §4.5 finding for the DC
 * shell (FE-14, "no focus traps") was fixed by retiring the DC shell, but
 * the React port's own ChatDrawer never got focus management either — it's
 * a floating panel toggled by a button, and neither opening nor closing it
 * moves keyboard focus anywhere. A screen-reader or keyboard-only user who
 * opens the drawer stays focused on the now-hidden toggle button, and
 * Tab/Shift+Tab from inside the drawer leaks focus into the page behind it.
 *
 * This hook, while `active`, moves focus into the container on open, cycles
 * Tab/Shift+Tab between the first and last focusable element inside it, and
 * restores focus to `restoreFocusRef` (or whatever was focused before
 * opening, if that ref isn't given) when `active` becomes false again —
 * mirrors the standard WAI-ARIA dialog pattern.
 */
export function useFocusTrap(
  active: boolean,
  containerRef: React.RefObject<HTMLElement | null>,
  options: FocusTrapOptions = {},
) {
  const { restoreFocusRef, onEscape } = options;
  const previouslyFocused = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!active) return;
    const container = containerRef.current;
    if (!container) return;

    previouslyFocused.current = document.activeElement as HTMLElement | null;

    const focusables = () => Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR));
    const first = focusables()[0];
    (first ?? container).focus();

    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape" && onEscape) {
        e.preventDefault();
        onEscape();
        return;
      }
      if (e.key !== "Tab") return;
      const items = focusables();
      if (items.length === 0) return;
      const firstEl = items[0];
      const lastEl = items[items.length - 1];
      if (e.shiftKey && document.activeElement === firstEl) {
        e.preventDefault();
        lastEl.focus();
      } else if (!e.shiftKey && document.activeElement === lastEl) {
        e.preventDefault();
        firstEl.focus();
      }
    }

    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      const target = restoreFocusRef?.current ?? previouslyFocused.current;
      target?.focus();
    };
    // Intentionally re-run only on `active`/container identity changes, not on
    // every restoreFocusRef/onEscape identity change — both are read via the
    // closure at effect-run time, and neither needs to retrigger the trap.
  }, [active, containerRef]);
}

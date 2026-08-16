import type { ReactNode } from "react";
import "./ui.css";
import { Button } from "./Button";

/**
 * T4-onboarding (docs/decisions — the "make the product explain itself" pass):
 * added `"not-configured"` and `"clean"` alongside the original three. A
 * fresh operator staring at a bare table cannot tell apart three very
 * different situations that `"none-yet"` used to flatten into one vague "no
 * data" message:
 *
 *   - not-configured — the collector needs credentials/env that were never
 *     set (e.g. Rapid7 for this homelab). No amount of waiting fixes this;
 *     the hint must name the exact setting.
 *   - none-yet        — the collector IS configured and scheduled, it just
 *     hasn't completed a run yet (or hasn't produced this particular slice).
 *     The hint should point at where the schedule/last-run lives.
 *   - clean            — the collector ran, found genuinely nothing to
 *     report, and that is GOOD news (e.g. zero open drift). Must not read
 *     like an error or a gap.
 *
 * `filter-zero` (current filters matched nothing) and `error` (the fetch
 * itself failed) are unchanged from before.
 */
export type EmptyStateKind = "none-yet" | "filter-zero" | "error" | "not-configured" | "clean";

export interface EmptyStateAction {
  label: string;
  onClick: () => void;
}

export interface EmptyStateProps {
  kind: EmptyStateKind;
  title: string;
  hint?: ReactNode;
  icon?: ReactNode;
  action?: EmptyStateAction;
  className?: string;
}

const DEFAULT_ICON: Record<EmptyStateKind, string> = {
  "none-yet": "○", // hollow circle — nothing collected yet
  "filter-zero": "⊘", // circled slash — filters matched nothing
  error: "✕", // heavy x — something actually failed
  "not-configured": "⚙", // gear — a setting was never supplied
  clean: "✓", // checkmark — ran, and genuinely found nothing to report
};

const KIND_CLASS: Record<EmptyStateKind, string | undefined> = {
  "none-yet": undefined,
  "filter-zero": undefined,
  error: "ui-empty-error",
  "not-configured": "ui-empty-not-configured",
  clean: "ui-empty-clean",
};

/**
 * The one component meant to replace this repo's ad-hoc empty-state handling
 * (see TRK-162..185 and e.g. `Fleet.tsx`/`Fleet.test.tsx`, which already
 * distinguish "Sites failed to load" (error) from "No Rapid7 sites collected
 * yet" (none-yet) from "No assets match the current filters" (filter-zero) —
 * that distinction was ad-hoc per-page before this; this component makes it
 * one contract with a `kind` prop instead of three copy-pasted patterns.
 */
export function EmptyState({ kind, title, hint, icon, action, className }: EmptyStateProps) {
  return (
    <div
      className={["ui-empty", KIND_CLASS[kind], className].filter(Boolean).join(" ")}
      role={kind === "error" ? "alert" : "status"}
      data-empty-kind={kind}
    >
      <div className="ui-empty-icon" aria-hidden="true">
        {icon ?? DEFAULT_ICON[kind]}
      </div>
      <div className="ui-empty-title">{title}</div>
      {hint ? <div className="ui-empty-hint">{hint}</div> : null}
      {action ? (
        <div className="ui-empty-action">
          <Button variant={kind === "error" ? "danger" : "secondary"} size="sm" onClick={action.onClick}>
            {action.label}
          </Button>
        </div>
      ) : null}
    </div>
  );
}

import type { ReactNode } from "react";
import "./ui.css";

export interface PageShellPill {
  dot: string;
  text: string;
}

export interface PageShellProps {
  title: string;
  description?: string;
  icon?: ReactNode;
  /** Optional row of colored-dot status pills, same shape as `PageHeader`'s
   *  `pills` prop for drop-in migration. */
  pills?: PageShellPill[];
  /** Explicit actions slot (e.g. a Refresh Button) — `PageHeader` today
   *  improvises this ad-hoc per page; PageShell makes it a real slot. */
  actions?: ReactNode;
  className?: string;
}

/**
 * Successor to `src/components/PageHeader.tsx` on the new dark design-system
 * tokens. `PageHeader` stays in place for pages not yet migrated (see that
 * file's docstring) — this is additive, not a replacement in place.
 */
export function PageShell({ title, description, icon, pills, actions, className }: PageShellProps) {
  return (
    <div className={["ui-page-shell", className].filter(Boolean).join(" ")}>
      {icon ? (
        <div className="ui-page-shell-icon" aria-hidden="true">
          {icon}
        </div>
      ) : null}
      <div className="ui-page-shell-body">
        <div className="ui-page-shell-title">{title}</div>
        {description ? <div className="ui-page-shell-desc">{description}</div> : null}
        {pills && pills.length > 0 ? (
          <div style={{ display: "flex", gap: 12, marginTop: 8, flexWrap: "wrap", alignItems: "center" }}>
            {pills.map((p) => (
              <span
                key={p.text}
                style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 11, color: "var(--muted)" }}
              >
                <span
                  style={{
                    width: 7,
                    height: 7,
                    borderRadius: "50%",
                    background: p.dot,
                    flexShrink: 0,
                  }}
                />
                {p.text}
              </span>
            ))}
          </div>
        ) : null}
      </div>
      {actions ? <div className="ui-page-shell-actions">{actions}</div> : null}
    </div>
  );
}

import type { ReactNode } from "react";
import "./ui.css";

/** Status rail hue. Maps to the four semantic colors; there is no "no rail"
 * state by design — a panel always declares how healthy its subject is. */
export type PanelRail = "ok" | "warn" | "err" | "info";

export interface PanelProps {
  /** 4px left status rail. Defaults to `info` (blue). */
  rail?: PanelRail;
  /** Short mono uppercase mnemonic, e.g. `VULN`, `DRIFT`, `TREND`, `GRAPH`. */
  mnemonic?: string;
  /** Subtitle describing the data behind the panel, e.g. `vulnerabilities ⨝ resources`. */
  description?: ReactNode;
  /** Render the green `● LIVE` marker (auto-refreshing data). */
  live?: boolean;
  /** Extra right-aligned header content (buttons, timestamps, counts). */
  headerRight?: ReactNode;
  /** Footer strip content — compose from `<PanelFootItem>`. */
  footer?: ReactNode;
  /** Accessible name for the landmark; defaults to `mnemonic`. */
  ariaLabel?: string;
  className?: string;
  id?: string;
  children?: ReactNode;
}

/**
 * The panel primitive from the approved mockup: a 4px semantic status rail, a
 * mono header (mnemonic + description + optional LIVE dot), the body, and an
 * optional footer strip.
 *
 * Header and footer are both optional — a bare `<Panel>` is just a railed,
 * bordered surface, which is what the widget components need.
 */
export function Panel({
  rail = "info",
  mnemonic,
  description,
  live = false,
  headerRight,
  footer,
  ariaLabel,
  className,
  id,
  children,
}: PanelProps) {
  const hasHeader = Boolean(mnemonic || description || live || headerRight);
  const name = ariaLabel ?? mnemonic;
  return (
    <section
      id={id}
      className={["ib-panel", className].filter(Boolean).join(" ")}
      aria-label={name}
      data-rail={rail}
    >
      <div className={`ib-rail ib-rail-${rail}`} />
      <div className="ib-panel-bd">
        {hasHeader && (
          <div className="ib-panel-hd">
            {mnemonic && <span className="ib-mn">{mnemonic}</span>}
            {description && <span className="ib-pd">{description}</span>}
            {headerRight && <span className="ib-panel-hd-right">{headerRight}</span>}
            {live && <span className="ib-live">LIVE</span>}
          </div>
        )}
        {children}
        {footer && <div className="ib-panel-ft">{footer}</div>}
      </div>
    </section>
  );
}

/** One item in a `Panel` footer strip. `tone` colors it warning/error. */
export function PanelFootItem({
  children,
  tone,
}: {
  children: ReactNode;
  tone?: "warn" | "err";
}) {
  const cls = tone === "warn" ? "ib-exit-warn" : tone === "err" ? "ib-exit-err" : undefined;
  return <span className={cls}>{children}</span>;
}

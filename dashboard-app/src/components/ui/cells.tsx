import type { MouseEvent, ReactNode } from "react";
import type { Severity } from "./SeverityPill";
import { normalizeSeverity } from "./SeverityPill";
import "./ui.css";

/** The dim-italic `NULL` token.
 *
 * Design contract: a missing value is *never* rendered as an empty cell —
 * "blank" is ambiguous between "no data collected" and "collected and empty".
 * `DataTable` applies this automatically whenever a cell's value (or a custom
 * `render`'s return) is `null`/`undefined`/`""`, so pages cannot forget it; this
 * component is exported for the rare cell rendered outside a `DataTable`. */
export function NullCell({ label = "NULL" }: { label?: string }) {
  return <span className="ib-null">{label}</span>;
}

/** True for values that must render as the NULL token rather than as content. */
export function isNullish(value: unknown): boolean {
  return value === null || value === undefined || (typeof value === "string" && value.trim() === "");
}

export interface FkCellProps {
  /** Human-readable label — the whole point of this cell is that a raw UUID is
   * never the primary text a user reads. */
  label: ReactNode;
  /** Optional dim technical id shown after the label (uuid, numeric id, ...). */
  id?: string | number | null;
  /** Href for a real navigable link. */
  href?: string;
  /** Click handler. Use this (with react-router's `navigate`) for in-app
   * navigation — these primitives deliberately don't depend on react-router so
   * they stay testable without a router wrapper. With `onClick` and no `href`
   * the cell renders a real <button>, so it stays keyboard-operable. */
  onClick?: (e: MouseEvent<HTMLElement>) => void;
  title?: string;
  className?: string;
}

/** Foreign-key style cell: human label + dim id + click-through arrow. */
export function FkCell({ label, id, href, onClick, title, className }: FkCellProps) {
  const cls = ["ib-fk", className].filter(Boolean).join(" ");
  const inner = (
    <>
      {label}
      {!isNullish(id) && <span className="ib-fkid">{String(id)}</span>}
    </>
  );
  if (href) {
    return (
      <a className={cls} href={href} onClick={onClick} title={title}>
        {inner}
      </a>
    );
  }
  if (onClick) {
    return (
      <button type="button" className={cls} onClick={onClick} title={title}>
        {inner}
      </button>
    );
  }
  return (
    <span className={cls} title={title}>
      {inner}
    </span>
  );
}

const GAUGE_CLASS: Record<Severity, string> = {
  critical: "ib-g-crit",
  high: "ib-g-high",
  medium: "ib-g-med",
  low: "ib-g-low",
};

export interface GaugeCellProps {
  /** Bar fill percentage, 0–100. Clamped. */
  value: number;
  /** Hue band for the fill. */
  severity: Severity | string | null | undefined;
  /** Text shown next to the bar. Defaults to the rounded `value`; pass the real
   * domain figure when the bar is a scaled view of it (e.g. CVSS 9.8 → 98%). */
  display?: ReactNode;
  /** Accessible description of what the gauge measures. */
  label?: string;
}

/** Bar-in-cell gauge for a bounded score (CVSS, coverage %, risk). */
export function GaugeCell({ value, severity, display, label }: GaugeCellProps) {
  const pct = Math.max(0, Math.min(100, Number.isFinite(value) ? value : 0));
  const level = normalizeSeverity(severity) ?? "low";
  const text = display ?? Math.round(pct);
  return (
    <span
      className={["ib-gauge", GAUGE_CLASS[level]].join(" ")}
      role="img"
      aria-label={label ? `${label}: ${text}` : `${text}`}
    >
      <span className="ib-bar">
        <i style={{ width: `${pct}%` }} />
      </span>
      {text}
    </span>
  );
}

/** Domain marker (`◆ windows`) — the mockup's `.dom` token. */
export function DomainCell({ name }: { name: string }) {
  return <span className="ib-dom">{name}</span>;
}

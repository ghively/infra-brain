import type { ReactNode } from "react";
import "./ui.css";

/** The four approved severity levels. The design contract is: CRITICAL is the
 * only *filled* pill (`--red-deep`), HIGH/MEDIUM are bordered in their hue, LOW
 * is bordered in muted grey. Nothing else in the app may invent a fifth level. */
export type Severity = "critical" | "high" | "medium" | "low";

const CLASS: Record<Severity, string> = {
  critical: "ib-sev-crit",
  high: "ib-sev-high",
  medium: "ib-sev-med",
  low: "ib-sev-low",
};

const LABEL: Record<Severity, string> = {
  critical: "Critical",
  high: "High",
  medium: "Medium",
  low: "Low",
};

/** Coerce whatever the API actually returned into one of the four levels.
 *
 * Backend severity strings are not consistent across domains (`Critical`,
 * `CRITICAL`, `crit`, `sev1`, `moderate`, ...), so normalization lives here
 * rather than being re-implemented per page. Anything unrecognized returns
 * `null` so the caller can render the raw string with LOW styling instead of
 * silently mislabeling it as low severity. */
export function normalizeSeverity(value: unknown): Severity | null {
  if (typeof value !== "string") return null;
  const v = value.trim().toLowerCase();
  if (v === "critical" || v === "crit" || v === "sev1" || v === "c") return "critical";
  // "severe" is the Rapid7 vuln-band vocabulary's name for this level (that
  // domain uses critical/severe/moderate, never "high" — see
  // src/infra_brain/db/models/rapid7.py's vuln_severe field docstring). Vulns.tsx
  // depends on this alias for severity-pill/gauge/rail accuracy (TRK-176/185).
  if (v === "high" || v === "severe" || v === "sev2" || v === "h") return "high";
  if (v === "medium" || v === "med" || v === "moderate" || v === "sev3" || v === "m") return "medium";
  if (v === "low" || v === "info" || v === "informational" || v === "sev4" || v === "l") return "low";
  return null;
}

export interface SeverityPillProps {
  /** A `Severity`, or any raw backend string — unrecognized values render with
   * LOW styling and keep their original text rather than being relabeled. */
  severity: Severity | string | null | undefined;
  /** Override the displayed text (CSS uppercases it either way). */
  label?: ReactNode;
  className?: string;
  title?: string;
}

export function SeverityPill({ severity, label, className, title }: SeverityPillProps) {
  const level = normalizeSeverity(severity);
  const cls = CLASS[level ?? "low"];
  const text =
    label ?? (level ? LABEL[level] : typeof severity === "string" && severity.trim() ? severity : "Unknown");
  return (
    <span className={["ib-sev", cls, className].filter(Boolean).join(" ")} title={title}>
      {text}
    </span>
  );
}

export interface BadgeProps {
  children: ReactNode;
  /** Semantic tone; maps onto the same four hues as the severity scale. */
  tone?: "neutral" | "info" | "ok" | "warn" | "err";
  className?: string;
  title?: string;
}

const TONE_CLASS: Record<NonNullable<BadgeProps["tone"]>, string> = {
  neutral: "ib-sev-low",
  info: "ib-sev-info",
  ok: "ib-sev-ok",
  warn: "ib-sev-med",
  err: "ib-sev-high",
};

/** Generic non-severity pill (status, state, count). Shares the severity pill's
 * geometry so badges and severities line up on the same row. */
export function Badge({ children, tone = "neutral", className, title }: BadgeProps) {
  return (
    <span className={["ib-sev", TONE_CLASS[tone], className].filter(Boolean).join(" ")} title={title}>
      {children}
    </span>
  );
}

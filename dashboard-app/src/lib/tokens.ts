/**
 * infra-brain dashboard design tokens — the single JS/TS source of truth.
 *
 * Phase 1 of the ground-up dashboard redesign (see `dashboard-app/DESIGN.md`
 * for the full contract, and `/home/youruser/.claude/plans/tidy-inventing-duckling.md`
 * for the plan this implements). Every value here has a matching CSS custom
 * property in `src/index.css`'s `:root` — this module exists so components that
 * still use inline `style={{}}` (the app's prevailing convention; there is no
 * CSS framework) can reach the same values without hardcoding hex literals.
 *
 * Rules for contributors:
 *  - Never introduce a new hex literal in a component. Add it here first, with a
 *    CSS var alongside it in index.css, or (much better) reuse an existing token.
 *  - Severity/status colors are derived through `severityStyle()` / `statusColor()`,
 *    never re-mapped by hand in a component. That mapping is the reason this
 *    module is the source of truth rather than a bag of constants.
 *  - There are exactly four semantic hues (red/yellow/green/blue) and no fifth
 *    "brand" accent. BLUE doubles as the only interactive accent (link, focus
 *    ring, selection) so it never competes with the semantic three.
 */

/**
 * Convert a `#rrggbb` hex string plus an alpha (0–1) into an `rgba(...)` string.
 * Kept verbatim from the pre-redesign token module — still the workhorse for
 * tinted backgrounds and borders derived from a solid token.
 */
export const hexA = (hex: string, a: number): string => {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
};

/* ------------------------------------------------------------------ *
 * Surfaces
 * ------------------------------------------------------------------ */

/** App background. Dark slate — deliberately NOT near-black. */
export const BG = "#1A1C21";
/** Default panel/card surface, one step up from BG. */
export const PANEL = "#22252C";
/** Raised surface inside a panel: table headers, toolbars, hovered rows. */
export const RAISED = "#262A32";
/** Structural border: panel edges, toolbar dividers, input outlines. */
export const BORDER = "#32363F";
/** Table cell gridlines specifically — dimmer than BORDER on purpose. */
export const GRID = "#2C3038";

/* ------------------------------------------------------------------ *
 * Text
 * ------------------------------------------------------------------ */

/** Primary text and data values. */
export const TEXT = "#D8DBE2";
/** Secondary text: column headers, labels, descriptions. */
export const MUTED = "#8A8FA0";
/** Tertiary text: NULL tokens, dim ids, units, footnotes. */
export const FAINT = "#6A6F7D";

/* ------------------------------------------------------------------ *
 * Semantic hues — exactly four, no fifth brand color
 * ------------------------------------------------------------------ */

/** Critical / error / failing. */
export const RED = "#F0654E";
/** Warning / degraded / stale. */
export const YELLOW = "#D9A62E";
/** OK / healthy / passing. */
export const GREEN = "#4CBB6C";
/** Info + the ONLY interactive accent: links, focus ring, selection. */
export const BLUE = "#5B9DE8";

/** Severity CRITICAL fill (filled pill background). */
export const RED_DEEP = "#8B2615";
/** Severity HIGH border (bordered pill outline). */
export const RED_MID = "#C0341D";

/** Foreground used on top of a filled RED_DEEP pill. */
export const ON_FILL = "#FFFFFF";

/* ------------------------------------------------------------------ *
 * Geometry + type
 * ------------------------------------------------------------------ */

/** Corner radius for panels and cards, in px. */
export const RADIUS = 8;
/** Corner radius for small inline chrome (pills, chips, buttons), in px. */
export const RADIUS_SM = 3;
/** Width of a Panel's colored status rail, in px. */
export const RAIL_WIDTH = 4;

/**
 * Font stacks. These reference the `@font-face` family names already vendored
 * as local woff2 in `index.css` — no CDN, no system-ui substitution.
 * DM Mono: all data, labels, and panel headers. DM Sans: body copy.
 */
export const FONT_MONO = '"DM Mono", ui-monospace, monospace';
export const FONT_SANS = '"DM Sans", system-ui, -apple-system, "Segoe UI", Roboto, sans-serif';

/* ------------------------------------------------------------------ *
 * Deprecated aliases — pre-redesign names, kept so unmigrated callers
 * compile. Do not use in new code; they resolve to new-system values.
 * ------------------------------------------------------------------ */

/** @deprecated Use {@link BLUE} — blue is the only interactive accent now. */
export const ACCENT = BLUE;
/** @deprecated Use {@link PANEL}. */
export const CARD_BG = PANEL;
/** @deprecated Use {@link BORDER}. */
export const CARD_BORDER = BORDER;

/* ------------------------------------------------------------------ *
 * Severity
 * ------------------------------------------------------------------ */

/**
 * The four severity levels. Intentionally identical to `RiskBand` in
 * `./riskBand` so a risk band can be fed straight into `severityStyle()`.
 */
export type Severity = "critical" | "high" | "medium" | "low";

/** Ordered worst → least severe. Useful for sorting and for `worstSeverity()`. */
export const SEVERITY_ORDER: readonly Severity[] = ["critical", "high", "medium", "low"] as const;

/** A resolved pill/badge palette: text color, background, border color. */
export interface SemanticStyle {
  /** Text/glyph color. */
  fg: string;
  /** Background fill (`"transparent"` for the outlined variants). */
  bg: string;
  /** Border color. */
  border: string;
}

/**
 * Severity → pill palette. This encodes the DESIGN.md severity color rule:
 *  - CRITICAL is the only *filled* level (RED_DEEP background, white text).
 *  - HIGH is transparent with a RED_MID border and RED text — the same hue
 *    family as critical, differentiated by weight rather than by a new hue.
 *  - MEDIUM/LOW step down to yellow and to neutral. There is never a fifth hue.
 */
export const SEMANTIC: Readonly<Record<Severity, SemanticStyle>> = {
  critical: { fg: ON_FILL, bg: RED_DEEP, border: RED_DEEP },
  high: { fg: RED, bg: "transparent", border: RED_MID },
  medium: { fg: YELLOW, bg: "transparent", border: hexA(YELLOW, 0.4) },
  low: { fg: MUTED, bg: "transparent", border: BORDER },
} as const;

/** The full pill palette for a severity. */
export function severityStyle(sev: Severity): SemanticStyle {
  return SEMANTIC[sev];
}

/**
 * Just the accent hue for a severity — for value coloring (StatTile numbers,
 * chart bars, rail tones) where a full pill palette is overkill.
 */
export function severityColor(sev: Severity): string {
  return sev === "critical" ? RED : SEMANTIC[sev].fg;
}

/**
 * Coerce an arbitrary backend string into a `Severity`, or `null` if it isn't
 * one. Case-insensitive, tolerates whitespace, and accepts the common aliases
 * the API layer emits (`crit`, `warning`, `moderate`, `info`, `none`).
 * Returns `null` rather than guessing so callers can render a NULL token
 * (see the NULL-token rule in DESIGN.md) instead of silently showing "low".
 */
export function normalizeSeverity(raw: string | null | undefined): Severity | null {
  if (raw == null) return null;
  const k = String(raw).trim().toLowerCase();
  if (!k) return null;
  switch (k) {
    case "critical":
    case "crit":
    case "fatal":
      return "critical";
    case "high":
    case "important":
    // "severe" is the Rapid7 vuln-band vocabulary's name for this level
    // (critical/severe/moderate, never "high" — see
    // src/infra_brain/db/models/rapid7.py's vuln_severe field docstring).
    case "severe":
      return "high";
    case "medium":
    case "med":
    case "moderate":
    case "warning":
    case "warn":
      return "medium";
    case "low":
    case "info":
    case "informational":
    case "none":
    case "negligible":
      return "low";
    default:
      return null;
  }
}

/**
 * Worst severity in a collection, or `null` if the collection is empty or holds
 * nothing recognizable. Drives a Panel's rail tone ("matching its worst
 * contained finding").
 */
export function worstSeverity(sevs: readonly (string | null | undefined)[]): Severity | null {
  let worst: Severity | null = null;
  let worstIdx = SEVERITY_ORDER.length;
  for (const raw of sevs) {
    const s = normalizeSeverity(raw);
    if (!s) continue;
    const i = SEVERITY_ORDER.indexOf(s);
    if (i < worstIdx) {
      worstIdx = i;
      worst = s;
    }
  }
  return worst;
}

/* ------------------------------------------------------------------ *
 * Status (health / run outcome)
 * ------------------------------------------------------------------ */

/**
 * Non-severity state: how a thing is *doing*, rather than how bad a finding is.
 * `neutral` covers unknown/not-applicable and renders in MUTED, never in a hue.
 */
export type Status = "ok" | "warn" | "error" | "info" | "neutral";

/** Status → accent hue. */
export const STATUS: Readonly<Record<Status, string>> = {
  ok: GREEN,
  warn: YELLOW,
  error: RED,
  info: BLUE,
  neutral: MUTED,
} as const;

/** Status → accent hue, with a defined fallback for unknown input. */
export function statusColor(status: Status | null | undefined): string {
  return status ? STATUS[status] : STATUS.neutral;
}

/** Status → tinted pill palette (outlined, hue-tinted background). */
export function statusStyle(status: Status | null | undefined): SemanticStyle {
  const c = statusColor(status);
  return { fg: c, bg: hexA(c, 0.12), border: hexA(c, 0.32) };
}

/**
 * Coerce an arbitrary backend status string into a `Status`. Unlike
 * `normalizeSeverity` this never returns `null` — an unrecognized status is
 * genuinely `neutral`, which is a real rendering, not a missing value.
 */
export function normalizeStatus(raw: string | null | undefined): Status {
  if (raw == null) return "neutral";
  const k = String(raw).trim().toLowerCase();
  switch (k) {
    case "ok":
    case "success":
    case "succeeded":
    case "healthy":
    case "pass":
    case "passed":
    case "completed":
    case "compliant":
    case "up":
    case "active":
      return "ok";
    case "warn":
    case "warning":
    case "degraded":
    case "stale":
    case "partial":
    case "pending":
      return "warn";
    case "error":
    case "failed":
    case "failure":
    case "fail":
    case "critical":
    case "down":
    case "noncompliant":
    case "non_compliant":
      return "error";
    case "info":
    case "running":
    case "in_progress":
    case "queued":
      return "info";
    default:
      return "neutral";
  }
}

/* ------------------------------------------------------------------ *
 * Panel rail
 * ------------------------------------------------------------------ */

/** The four rail tones a `Panel`'s 4px left edge can take. */
export type RailTone = "green" | "yellow" | "red" | "blue";

/** Rail tone → color. */
export const RAIL: Readonly<Record<RailTone, string>> = {
  green: GREEN,
  yellow: YELLOW,
  red: RED,
  blue: BLUE,
} as const;

/** Rail tone → color, defaulting to the informational blue. */
export function railColor(tone: RailTone | null | undefined): string {
  return tone ? RAIL[tone] : RAIL.blue;
}

/**
 * Severity → rail tone. `critical`/`high` both read red (the rail has no
 * fill-vs-outline distinction to spend on that difference); `medium` is yellow;
 * `low` and "no findings at all" are green — a panel with only low findings is
 * a healthy panel.
 */
export function severityRail(sev: Severity | null | undefined): RailTone {
  switch (sev) {
    case "critical":
    case "high":
      return "red";
    case "medium":
      return "yellow";
    case "low":
      return "green";
    default:
      return "green";
  }
}

/** Status → rail tone, for panels keyed on health rather than on findings. */
export function statusRail(status: Status | null | undefined): RailTone {
  switch (normalizeStatus(status)) {
    case "ok":
      return "green";
    case "warn":
      return "yellow";
    case "error":
      return "red";
    default:
      return "blue";
  }
}

/* ------------------------------------------------------------------ *
 * DataTable contracts
 * ------------------------------------------------------------------ */

/** Row-density modes, surfaced as a `data-density` attribute on the table. */
export type Density = "compact" | "cozy" | "tall";

/** The default density for a new DataTable. */
export const DEFAULT_DENSITY: Density = "cozy";

/** All densities, in toolbar-toggle order. */
export const DENSITIES: readonly Density[] = ["compact", "cozy", "tall"] as const;

/** Vertical cell padding per density, in px. Mirrors the CSS in index.css. */
export const DENSITY_PADDING: Readonly<Record<Density, number>> = {
  compact: 3,
  cozy: 7,
  tall: 12,
} as const;

/** Column data kinds, rendered as a type glyph in the DataTable header. */
export type ColumnKind = "pk" | "text" | "num" | "enum" | "fk";

/** Column kind → header glyph. See the type-glyph legend in DESIGN.md. */
export const TYPE_GLYPH: Readonly<Record<ColumnKind, string>> = {
  pk: "PK",
  text: "T",
  num: "#",
  enum: "◆",
  fk: "⇥",
} as const;

/** The literal text a NULL/missing cell renders as (dim + italic, never blank). */
export const NULL_TOKEN = "NULL";

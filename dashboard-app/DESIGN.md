# infra-brain Dashboard — Design System Contract

**Status:** Phase 1 foundation, established 2026-07-27. This document is a
*contract*, not a suggestion — every page rebuilt in Phase 2 onward, and every
new component, is expected to conform. If a page needs to break a rule here,
change the rule in this file first (with reasoning) so the next contributor
inherits the decision rather than the exception.

**Source of truth pairing:**

| Layer | File | Use from |
|---|---|---|
| TS/JS constants + severity logic | `src/lib/tokens.ts` | inline `style={{}}`, component logic |
| CSS custom properties | `src/index.css` `:root` | `.ib-*` classes, any stylesheet rule |

The two must always agree. Change both in the same commit.

---

## 1. Why there is no CSS framework

There is deliberately no Tailwind, CSS Modules, styled-components, or charting
library. Styling is **plain CSS classes in `src/index.css` (prefixed `.ib-*`)
plus inline `style={{}}` reading `src/lib/tokens.ts`**. This is what the codebase
already did, what the approved mockup was built in, and it avoids adding tooling
risk mid-rewrite. Charts and widgets are hand-rolled divs and inline SVG.

Corollary rule: **never write a new hex literal in a component.** Add a token
here first, or reuse one. The pre-redesign app had 63 ad-hoc hex colors against
5 real tokens — that is the specific failure this system exists to prevent.

---

## 2. Tokens

### Surfaces

| Token (TS) | CSS var | Value | Use for |
|---|---|---|---|
| `BG` | `--ib-bg` | `#1A1C21` | App background. Dark slate — **not** near-black. |
| `PANEL` | `--ib-panel` | `#22252C` | Panel/card surface, one step up from `BG`. |
| `RAISED` | `--ib-raised` | `#262A32` | Raised surfaces *inside* a panel: sticky table headers, toolbars, hovered rows, input fields. |
| `BORDER` | `--ib-border` | `#32363F` | Structural borders: panel edges, toolbar dividers, input outlines. |
| `GRID` | `--ib-grid` | `#2C3038` | Table cell gridlines **only** — intentionally dimmer than `BORDER` so a dense grid doesn't read as noise. |

### Text

| Token | CSS var | Value | Use for |
|---|---|---|---|
| `TEXT` | `--ib-text` | `#D8DBE2` | Primary text and all data values. |
| `MUTED` | `--ib-muted` | `#8A8FA0` | Column headers, labels, descriptions, secondary copy. |
| `FAINT` | `--ib-faint` | `#6A6F7D` | `NULL` tokens, dim ids next to FK labels, units, footnotes, disabled state. |

### Semantic hues — exactly four

| Token | CSS var | Value | Meaning |
|---|---|---|---|
| `RED` | `--ib-red` | `#F0654E` | Critical / error / failing. |
| `YELLOW` | `--ib-yellow` | `#D9A62E` | Warning / degraded / stale. |
| `GREEN` | `--ib-green` | `#4CBB6C` | OK / healthy / passing. |
| `BLUE` | `--ib-blue` | `#5B9DE8` | Info **and** the only interactive accent: links, focus ring, selection, primary buttons. |
| `RED_DEEP` | `--ib-red-deep` | `#8B2615` | Severity CRITICAL fill. Never used as a text color. |
| `RED_MID` | `--ib-red-mid` | `#C0341D` | Severity HIGH border. Never used as a text color. |
| `ON_FILL` | `--ib-on-fill` | `#FFFFFF` | Text on top of a filled `RED_DEEP` pill. |

**There is no fifth "brand" accent.** The old indigo `#6366f1` is retired. Blue
carries the interactive role precisely so a link or a focused input can never be
mistaken for a semantic state, and so no fifth hue competes with the three that
carry meaning.

### Geometry

| Token | CSS var | Value | Use for |
|---|---|---|---|
| `RADIUS` | `--ib-radius` | `8px` | Panels and cards. |
| `RADIUS_SM` | `--ib-radius-sm` | `3px` | Pills, chips, buttons, inputs, focus ring. |
| `RAIL_WIDTH` | `--ib-rail-w` | `4px` | A Panel's status rail. |

### Variable-naming decision (read this before adding CSS)

The `:root` variables were **replaced in place**, not renamed alongside the old
set. Reasoning, recorded because it constrains Phase 2+:

- Every existing page already reads `--ib-bg`/`--ib-panel`/`--ib-border`/
  `--ib-text`/`--ib-muted`. A parallel new-prefixed set would have left all 39
  pages on a dead palette, with no forcing function to ever migrate them.
- All old→new pairs move dark→dark and light-text→light-text, so nothing loses
  legibility: `#070b16`→`#1A1C21` (bg), `#0b0f1c`→`#22252C` (panel),
  `#d4dcf0`→`#D8DBE2` (text), `#6b7a99`→`#8A8FA0` (muted),
  `#1e2d47`→`#32363F` (border). Unmigrated pages remain fully usable.
- **Accepted cost:** the ~2.3k inline-hardcoded indigo/blue-black literals still
  in unmigrated pages now read slightly off-family against slate surfaces. This
  is expected during the phased rollout and resolves page-by-page as Phase 2–5
  rebuild them. Do not chase it as a bug.

Legacy var names that had no new-system twin (`--ib-panel-2`, `--ib-border-soft`,
`--ib-text-strong`, `--ib-muted-dim`, `--ib-accent`, `--ib-accent-soft`,
`--ib-body-bg`) were **retuned onto the new palette and kept**. They are
deprecated: do not use them in new code, and delete each one as its last caller
is rebuilt. The TS side mirrors this — `ACCENT`, `CARD_BG`, `CARD_BORDER` remain
as `@deprecated` aliases pointing at `BLUE`/`PANEL`/`BORDER`.

Bare aliases (`--bg`, `--panel`, `--red-deep`, …) also exist, mapped via
`var()` to their `--ib-*` twins. Their only purpose is to let CSS ported
verbatim from the approved mockup work unedited. **Prefer `--ib-*` in new app
CSS**; never give an alias a value its `--ib-*` twin lacks.

---

## 3. Severity color rule

Four levels: `critical | high | medium | low` (`Severity` in `tokens.ts`,
identical to `RiskBand` in `lib/riskBand.ts` so risk bands feed straight in).

| Level | Background | Border | Text |
|---|---|---|---|
| CRITICAL | filled `--ib-red-deep` | `--ib-red-deep` | white (`--ib-on-fill`) |
| HIGH | transparent | `--ib-red-mid` | `--ib-red` |
| MEDIUM | transparent | `--ib-yellow` @ 40% | `--ib-yellow` |
| LOW | transparent | `--ib-border` | `--ib-muted` |

**Critical and high share the red hue family** and are distinguished by *weight*
— filled vs. outlined — not by introducing a fifth color. A filled pill is
visually heavy and should therefore be rare on a page; that scarcity is the
point.

Consume this through `tokens.ts`, never by re-deriving it:

```ts
import { severityStyle, severityColor, normalizeSeverity, worstSeverity } from "../lib/tokens";

severityStyle("critical")   // → { fg, bg, border } for a pill
severityColor("high")       // → single accent hue, for a StatTile value or bar
normalizeSeverity(row.sev)  // → Severity | null  (null ⇒ render a NULL token)
worstSeverity(rows.map(r => r.severity)) // → Severity | null, for a Panel rail
```

`normalizeSeverity` returns `null` for unrecognized input rather than guessing
`"low"` — an unknown severity is missing data and must render as a NULL token,
not as a reassuring green.

**Status is a separate axis.** `Status = ok | warn | error | info | neutral`
describes how a thing is *doing* (run outcome, host health), not how bad a
finding is. Use `normalizeStatus` / `statusColor` / `statusStyle`. Unlike
severity, `normalizeStatus` never returns `null` — unknown genuinely *is*
`neutral`, and neutral renders in `MUTED`, never in a hue.

---

## 4. NULL-token rule

**A missing value never renders as a blank cell.** A blank cell is ambiguous —
it could mean "no data", "zero", "not applicable", or "the renderer broke".

Render `NULL` (the `NULL_TOKEN` constant) in `--ib-faint`, `font-style: italic`,
DM Mono. Same treatment for empty strings and empty arrays. If a value is
legitimately not applicable rather than unknown, render an em-dash `—` in
`--ib-faint` instead, so the two cases stay distinguishable.

Zero is a value, not a NULL. Render `0` normally.

---

## 5. FK-as-human-label rule

**A raw UUID is never the primary content of a cell.** A foreign-key-style
reference renders three parts:

1. The related record's **human-readable label** in `--ib-text` (hostname, CVE
   id, run name) — this is what the user actually reads.
2. A **dim id** in `--ib-faint`, DM Mono, truncated (first 8 chars is enough) —
   present for copy/paste and support conversations, not for reading.
3. A **click-through affordance** (`→`) navigating to the related record.

If no human label is resolvable, show the full id plus an explicit unresolved
marker — do not silently present the id as if it were a name.

---

## 6. Panel and rail system

A `Panel` is the standard content container:

- Surface `--ib-panel`, `1px solid --ib-border`, radius `--ib-radius`.
- A **4px colored status rail** on the left edge (`--ib-rail-w`), tone
  `green | yellow | red | blue` (`RailTone`).
- Header: a short **mono mnemonic** in DM Mono uppercase (`VULN`, `DRIFT`,
  `TREND`, `GRAPH`), then a `--ib-muted` description, then optional right-aligned
  chrome (a `LIVE` dot, a count, an action).

**The rail tone matches the panel's worst contained finding**, so page health is
scannable from the left margin alone without reading any panel.

```ts
severityRail(worstSeverity(findings.map(f => f.severity)))  // findings-based
statusRail(run.status)                                      // health-based
```

Mapping: `critical`/`high` → red; `medium` → yellow; `low` or no findings →
green; unknown/informational → blue. A panel whose worst finding is `low` is a
healthy panel and reads green.

---

## 7. DataTable

### Type-glyph legend

Every column header carries a small `--ib-faint` mono glyph declaring the
column's data kind (`ColumnKind` / `TYPE_GLYPH`):

| Glyph | Kind | Meaning |
|---|---|---|
| `PK` | `pk` | Primary key / unique identifier for the row. |
| `T` | `text` | Free text. |
| `#` | `num` | Numeric — right-aligned, `font-variant-numeric: tabular-nums`, DM Mono. |
| `◆` | `enum` | Enum / constrained domain — a fixed known value set. |
| `⇥` | `fk` | Foreign key / join — renders per the FK rule in §5. |

The glyphs are a deliberate database-client affordance: they tell the operator
what a column *is* before they read a single row, and they make an
inappropriately-typed cell renderer obvious at a glance.

### Row-density contract

Three modes (`Density`): `compact | cozy | tall`, default **`cozy`**.

Implemented as a `data-density` attribute on the table (or its wrapper), with
CSS keyed off the attribute selector — **not** as inline per-row padding:

```html
<table class="ib-datatable" data-density="compact">
```

```css
.ib-datatable[data-density="compact"] td { padding-top: 3px; padding-bottom: 3px; }
.ib-datatable[data-density="cozy"]    td { padding-top: 7px; padding-bottom: 7px; }
.ib-datatable[data-density="tall"]    td { padding-top: 12px; padding-bottom: 12px; }
```

`DENSITY_PADDING` in `tokens.ts` mirrors those px values for any component that
needs them in JS. The toggle must be a real working control, not decorative, and
the chosen density should persist for the user's session.

### Other required behaviors

- Full gridlines in `--ib-grid`; sticky header on `--ib-raised` in DM Mono.
- Toolbar: filter chips + the density toggle.
- Severity cells → severity pills per §3. NULL cells → §4. FK cells → §5.
- Numeric score columns (CVSS-style) may render an in-cell gauge bar.
- Status-bar footer: `N of TOTAL rows · Xms · read-only`. The `read-only` marker
  is not decoration — infra-brain never mutates infrastructure, and the UI says
  so on every data surface.

---

## 8. Table vs. card vs. widget

Pick by **what the user is trying to do**, not by what looks nicer:

**Table** — comparing many records across the same fields, or looking for a
specific record. The default for any collection of >5 homogeneous rows. Never
render a list of records as a stack of cards to look modern; that destroys
column alignment, which is the entire value of tabular data.

**Card / Panel** — one entity's summary, or a small heterogeneous group of
related facts that don't share a column shape. Cards are for *reading*; tables
are for *scanning*. Also correct for a small (≤5) set of top-level entry points.

**Widget** — one volatile metric over time, or a relationship, where the *shape*
carries the meaning: cockpit-tape instrument (zone-banded ribbon + trend vector
+ "now" marker) for a single volatile metric; stacked-bar trend chart for a
composition changing over time; SVG relationship mini-graph (hub + satellites +
edges) for adjacency and blast radius. A widget is justified only when the
visual form communicates something a number in a `StatTile` cannot. One number
with no trend and no relationship is a `StatTile`, not a widget.

**StatTile** — a single headline number plus a label and optional delta. Value
colored via `severityColor` / `statusColor`; never colored by hand.

---

## 9. Fonts

Both families are **vendored locally as woff2** in `src/assets/fonts/` and
declared via `@font-face` at the top of `src/index.css`. There is no CDN and no
external origin — do not add one, and do not fall back to a `system-ui` stack.

Reference them by the exact `@font-face` family names:

```css
font-family: "DM Mono", ui-monospace, monospace;   /* --ib-mono equivalent */
font-family: "DM Sans", system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
```

or from TS: `FONT_MONO` / `FONT_SANS` in `tokens.ts`.

**DM Mono** (weights 400, 500) — all **data**: table cells, ids, numbers,
timestamps, hostnames, panel-header mnemonics, column headers, glyphs, chips,
status-bar footers. Anything an operator might compare character-by-character or
copy out.

**DM Sans** (weights 400, 500, 600, 700) — **body copy**: page descriptions,
help text, empty-state prose, button labels, dialog text.

The split is functional, not decorative: mono means "this is data, aligned and
comparable"; sans means "this is prose, read it".

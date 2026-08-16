/** Shared helpers for the consolidated Hosts page (Phase 2 of the ground-up
 *  dashboard redesign — see `dashboard-app/DESIGN.md` and
 *  `/home/youruser/.claude/plans/tidy-inventing-duckling.md`).
 *
 *  `Hosts.tsx`, `Resources.tsx` and `Fleet.tsx` each hand-rolled their own
 *  filter chips, pagers, search boxes and drawer field grids with inline hex
 *  literals. Those five patterns are consolidated here once, reading
 *  `lib/tokens.ts` so no component in this directory writes a hex literal
 *  (DESIGN.md §1 corollary).
 *
 *  Imports of the Phase 1 primitives in this directory deliberately reach the
 *  individual modules (`components/ui/Button`) rather than the `components/ui`
 *  barrel: the barrel only re-exports Panel/DataTable/SeverityPill/cells today,
 *  and concurrent Phase 2 page rebuilds are touching that shared file — direct
 *  module imports keep this work conflict-free.
 */
import type { ReactNode } from "react";
import { Button } from "../../components/ui/Button";
import { BORDER, FAINT, FONT_MONO, MUTED, RAISED, TEXT } from "../../lib/tokens";
import { fmtDt as fmtDtShared } from "../../lib/fmtDt";

/** Relative "12m ago" / "3h ago" / "3d 6h ago" stamp. `null` renders as an
 *  em-dash (the not-applicable form from DESIGN.md §4, distinct from the NULL
 *  token).
 *
 *  F-11: two edge cases the naive `(Date.now() - t) / 60000` version got
 *  wrong: (1) a future timestamp (clock skew between the collector host and
 *  the browser) produced a negative minute count and rendered as e.g.
 *  "-3m ago" instead of clamping to "just now"; (2) anything over 24h
 *  rendered as an ever-growing hour count ("78h ago") with no day unit,
 *  unlike every other duration bucket here which rolls over into the next
 *  unit. */
export function relTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const diff = Date.now() - new Date(iso).getTime();
  if (diff < 0) return "just now";
  const m = Math.floor(diff / 60000);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  const remH = h % 24;
  return remH > 0 ? `${d}d ${remH}h ago` : `${d}d ago`;
}

/** `2026-07-27 13:04:11` from an ISO stamp; `undefined` (not "—") for a
 *  missing value, delegating to the canonical `lib/fmtDt.ts` so this
 *  directory's NULL-token contract (undefined → DataTable's own NULL cell)
 *  matches every other consumer of the shared helper. Re-exported here
 *  because R7Tabs.tsx / ResourcesTab.tsx import it from this module. */
export const fmtDt = fmtDtShared;

/** Build a `?a=1&b=2` query string, dropping empty values. */
export function qs(params: Record<string, string | number>): string {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== "" && v !== undefined && v !== null) sp.set(k, String(v));
  }
  const s = sp.toString();
  return s ? `?${s}` : "";
}

/** `Yes` / `No` / em-dash for a nullable boolean. */
export function fmtBool(b: boolean | null | undefined): string {
  if (b === null || b === undefined) return "—";
  return b ? "Yes" : "No";
}

export interface ChipOption {
  value: string;
  label: string;
}

/** A row of single-select filter chips. Built on the shared `Button` primitive
 *  (primary = selected) rather than a fourth hand-rolled pill style. */
export function ChipFilter({
  label,
  options,
  active,
  onChange,
}: {
  label: string;
  options: ChipOption[];
  active: string;
  onChange: (value: string) => void;
}) {
  return (
    <div role="group" aria-label={label} style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
      {options.map((o) => (
        <Button
          key={o.value}
          size="sm"
          variant={active === o.value ? "primary" : "ghost"}
          aria-pressed={active === o.value}
          onClick={() => onChange(o.value)}
        >
          {o.label}
        </Button>
      ))}
    </div>
  );
}

/** Search / free-text filter input on the design-system surfaces. */
export function FilterInput({
  value,
  onChange,
  placeholder,
  label,
  width = 210,
  defaultValue,
}: {
  value?: string;
  defaultValue?: string;
  onChange: (value: string) => void;
  placeholder: string;
  label: string;
  width?: number;
}) {
  return (
    <input
      aria-label={label}
      value={value}
      defaultValue={defaultValue}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      style={{
        background: RAISED,
        border: `1px solid ${BORDER}`,
        borderRadius: 3,
        padding: "5px 10px",
        color: TEXT,
        fontFamily: FONT_MONO,
        fontSize: 12,
        outline: "none",
        width,
      }}
    />
  );
}

/** `1–100 of 4,213 hosts` window label for an offset/limit paged endpoint. */
export function pageWindow(offset: number, shown: number, total: number, noun: string): string {
  if (total === 0) return `0 ${noun}`;
  return `${offset + 1}–${Math.min(offset + shown, total)} of ${total} ${noun}`;
}

/** Prev/Next pager. Buttons are omitted (not disabled) at the ends, matching
 *  the behavior all three original pages had. */
export function Pager({
  offset,
  shown,
  total,
  pageSize,
  noun,
  onOffset,
}: {
  offset: number;
  shown: number;
  total: number;
  pageSize: number;
  noun: string;
  onOffset: (offset: number) => void;
}) {
  const hasPrev = offset > 0;
  const hasNext = offset + shown < total;
  if (!hasPrev && !hasNext) return null;
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 10, marginTop: 12 }}>
      {hasPrev && (
        <Button size="sm" onClick={() => onOffset(Math.max(0, offset - pageSize))}>
          ‹ Prev
        </Button>
      )}
      <span style={{ fontSize: 11, fontFamily: FONT_MONO, color: MUTED }}>
        {pageWindow(offset, shown, total, noun)}
      </span>
      {hasNext && (
        <Button size="sm" onClick={() => onOffset(offset + pageSize)}>
          Next ›
        </Button>
      )}
    </div>
  );
}

/** Uppercase mono section label used inside the detail drawers. */
export function SectionLabel({ children }: { children: ReactNode }) {
  return (
    <div
      style={{
        fontSize: 10,
        fontWeight: 700,
        fontFamily: FONT_MONO,
        color: MUTED,
        textTransform: "uppercase",
        letterSpacing: ".1em",
        margin: "0 0 10px",
      }}
    >
      {children}
    </div>
  );
}

export interface KvField {
  label: string;
  value: ReactNode;
}

/** Label-over-value grid for a small heterogeneous fact set (DESIGN.md §8:
 *  a card is for reading, a table is for scanning — drawer facts are reading). */
export function KvGrid({ fields, columns = 3 }: { fields: KvField[]; columns?: number }) {
  return (
    <div style={{ display: "grid", gridTemplateColumns: `repeat(${columns},minmax(0,1fr))`, gap: 14 }}>
      {fields.map((f) => (
        <div key={f.label} style={{ minWidth: 0 }}>
          <div
            style={{
              fontSize: 10,
              fontWeight: 700,
              fontFamily: FONT_MONO,
              color: MUTED,
              textTransform: "uppercase",
              letterSpacing: ".08em",
            }}
          >
            {f.label}
          </div>
          <div
            style={{
              fontSize: 13,
              fontFamily: FONT_MONO,
              color: TEXT,
              marginTop: 3,
              wordBreak: "break-word",
            }}
          >
            {f.value}
          </div>
        </div>
      ))}
    </div>
  );
}

/** Inline hint/footnote line inside a drawer section. */
export function DrawerNote({ children, tone }: { children: ReactNode; tone?: "warn" | "err" }) {
  const color = tone === undefined ? FAINT : `var(--${tone === "warn" ? "yellow" : "red"})`;
  return <div style={{ marginTop: 10, fontSize: 11, color }}>{children}</div>;
}

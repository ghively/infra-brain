import { isValidElement, useId, useMemo, useState } from "react";
import type { ReactNode } from "react";
import type { SortDir } from "../../hooks/useSort";
import { NullCell, isNullish } from "./cells";
import "./ui.css";

/** Column type glyph shown in the sticky header, per the mockup's legend.
 *  `pk`   → PK  (filled — the row's identity)
 *  `text` → T
 *  `num`  → #   (also right-aligns the column by default)
 *  `enum` → ◆   (bounded set: domain, status, severity)
 *  `fk`   → ⇥   (points at another entity) */
export type TypeGlyph = "pk" | "text" | "num" | "enum" | "fk";

const GLYPH: Record<TypeGlyph, string> = {
  pk: "PK",
  text: "T",
  num: "#",
  enum: "◆",
  fk: "⇥",
};

const GLYPH_TITLE: Record<TypeGlyph, string> = {
  pk: "primary key",
  text: "text",
  num: "numeric",
  enum: "enumerated value",
  fk: "reference to another record",
};

export type Density = "compact" | "cozy" | "tall";

export const DENSITIES: { value: Density; label: string }[] = [
  { value: "compact", label: "Compact" },
  { value: "cozy", label: "Cozy" },
  { value: "tall", label: "Tall" },
];

export interface ColumnDef<T> {
  /** Stable column id. Also the default value accessor (`row[key]`). */
  key: string;
  /** Header text. */
  header: string;
  /** Type glyph chip in the header. Defaults to `text`. */
  typeGlyph?: TypeGlyph;
  /** Cell alignment. Defaults to `right` for `num`, `left` otherwise. */
  align?: "left" | "right";
  /** Custom cell content. Returning `null`/`undefined`/`""` still renders the
   * NULL token — the null contract cannot be bypassed by accident. */
  render?: (row: T, rowIndex: number) => ReactNode;
  /** Value extraction for the default renderer *and* for sorting. Defaults to
   * `(row) => (row as Record<string, unknown>)[key]`. */
  accessor?: (row: T) => unknown;
  /** Set `false` to exclude this column from sorting when the table has
   * `sortable` on (e.g. a column whose cell is a button). Default: sortable. */
  sortable?: boolean;
  /** Fixed column width. */
  width?: number | string;
}

export interface StatusBarInfo {
  /** Rows currently rendered. Defaults to `rows.length`. */
  shown?: number;
  /** Total rows matching the query server-side (may exceed `shown`). */
  total?: number;
  /** Query duration in ms. */
  ms?: number;
  /** Right-aligned note. Defaults to the read-only reassurance from the mockup. */
  note?: ReactNode;
}

export interface DataTableProps<T> {
  columns: ColumnDef<T>[];
  rows: T[];
  /** Stable React key per row. Defaults to the row index. */
  rowKey?: (row: T, index: number) => string | number;
  /** Initial row height when uncontrolled. Defaults to `compact`. */
  defaultDensity?: Density;
  /** Controlled density. When set, `onDensityChange` must update it. */
  density?: Density;
  onDensityChange?: (density: Density) => void;
  /** Hide the Compact/Cozy/Tall segmented control. Defaults to shown. */
  showDensityToggle?: boolean;
  /** Bold mono table name in the toolbar. */
  toolbarName?: ReactNode;
  /** Filter/context chips in the toolbar, e.g. `["severity ≥ medium"]`. */
  chips?: ReactNode[];
  /** Extra toolbar content, placed before the density control. */
  toolbarRight?: ReactNode;
  /** Footer status strip. Pass `false` to omit. */
  statusBar?: StatusBarInfo | false;
  /** Enable click-to-sort on columns marked `sortable`. */
  sortable?: boolean;
  /** Initial sort column key. */
  initialSortKey?: string;
  initialSortDir?: SortDir;
  /** Table min-width before horizontal scrolling kicks in. Mockup default 1020. */
  minWidth?: number | string;
  /** Shown in place of the body when `rows` is empty. */
  emptyMessage?: ReactNode;
  /** Row click handler (whole-row drill-in). */
  onRowClick?: (row: T, index: number) => void;
  /** Screen-reader table caption. Strongly recommended. */
  caption?: string;
  className?: string;
}

/** Comparator matching `hooks/useSort`'s semantics exactly: nulls last, numeric
 * compare for numbers, natural/numeric string collation otherwise.
 *
 * `useSort` itself is keyed on `keyof T` and so cannot sort an accessor-derived
 * column (a `ColumnDef` may compute its value), which is why the comparison is
 * reimplemented here rather than reused. Keep the two in step.
 *
 * `dir` only flips the ordering of two *non-null* values — nulls must stay
 * last in both directions, so it cannot be implemented as "sort ascending,
 * then reverse the whole array" (that would put nulls first on descending). */
function compareValues(av: unknown, bv: unknown, dir: SortDir): number {
  if (av == null && bv == null) return 0;
  if (av == null) return 1;
  if (bv == null) return -1;
  const cmp =
    typeof av === "number" && typeof bv === "number"
      ? av - bv
      : String(av).localeCompare(String(bv), undefined, { numeric: true });
  return dir === "desc" ? -cmp : cmp;
}

function readValue<T>(col: ColumnDef<T>, row: T): unknown {
  return col.accessor ? col.accessor(row) : (row as Record<string, unknown>)[col.key];
}

/** Default cell rendering for a raw accessor value. Deliberately total — an
 * unexpected object/Date must not throw "Objects are not valid as a React
 * child" and take the whole page's error boundary down over one bad column. */
function defaultCell(value: unknown): ReactNode {
  if (isNullish(value)) return null;
  if (typeof value === "string" || typeof value === "number") return value;
  if (typeof value === "boolean") return value ? "true" : "false";
  if (isValidElement(value)) return value;
  if (value instanceof Date) return value.toISOString();
  return String(value);
}

/**
 * The table primitive from the approved mockup: grid-lined cells, sticky mono
 * header with a per-column type glyph, toolbar with filter chips and a working
 * row-density toggle, and a status-bar footer.
 *
 * Two contracts are enforced by the component rather than left to call sites:
 * missing values always render the dim-italic NULL token (never a blank cell),
 * and the density toggle is real React state written onto the wrapper as
 * `data-density` (the CSS keys off it to change actual cell padding).
 */
export function DataTable<T>({
  columns,
  rows,
  rowKey,
  defaultDensity = "compact",
  density: controlledDensity,
  onDensityChange,
  showDensityToggle = true,
  toolbarName,
  chips,
  toolbarRight,
  statusBar,
  sortable = false,
  initialSortKey,
  initialSortDir = "asc",
  minWidth = 1020,
  emptyMessage = "No rows.",
  onRowClick,
  caption,
  className,
}: DataTableProps<T>) {
  const [uncontrolledDensity, setUncontrolledDensity] = useState<Density>(defaultDensity);
  const density = controlledDensity ?? uncontrolledDensity;

  const [sortKey, setSortKey] = useState<string | null>(initialSortKey ?? null);
  const [sortDir, setSortDir] = useState<SortDir>(initialSortDir);

  const densityLabelId = useId();

  function setDensity(next: Density) {
    if (controlledDensity === undefined) setUncontrolledDensity(next);
    onDensityChange?.(next);
  }

  function toggleSort(key: string) {
    if (key === sortKey) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir("asc");
    }
  }

  const sortedRows = useMemo(() => {
    if (!sortable || !sortKey) return rows;
    const col = columns.find((c) => c.key === sortKey);
    if (!col) return rows;
    const copy = rows.slice();
    copy.sort((a, b) => compareValues(readValue(col, a), readValue(col, b), sortDir));
    return copy;
  }, [rows, columns, sortable, sortKey, sortDir]);

  const hasToolbar = Boolean(toolbarName || (chips && chips.length) || toolbarRight || showDensityToggle);

  const sb: StatusBarInfo | null = statusBar === false || statusBar === undefined ? null : statusBar;

  return (
    <div className={["ib-dt", className].filter(Boolean).join(" ")} data-density={density}>
      {hasToolbar && (
        <div className="ib-toolbar">
          {toolbarName && <span className="ib-tname">{toolbarName}</span>}
          {chips?.map((chip, i) => (
            <span className="ib-tchip" key={i}>
              {chip}
            </span>
          ))}
          {toolbarRight}
          {showDensityToggle && (
            <div className="ib-density">
              <span className="ib-dlbl" id={densityLabelId}>
                Row height
              </span>
              <div className="ib-seg" role="group" aria-labelledby={densityLabelId}>
                {DENSITIES.map((d) => (
                  <button
                    key={d.value}
                    type="button"
                    data-dens={d.value}
                    aria-pressed={density === d.value}
                    onClick={() => setDensity(d.value)}
                  >
                    {d.label}
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      <div className="ib-tbl-scroll">
        <table className="ib-tbl" style={{ minWidth }}>
          {caption && <caption className="ib-sr-only">{caption}</caption>}
          <thead>
            <tr>
              {columns.map((col) => {
                const glyph = col.typeGlyph ?? "text";
                const align = col.align ?? (glyph === "num" ? "right" : "left");
                const canSort = sortable && col.sortable !== false;
                const active = sortKey === col.key;
                const chip = (
                  <span
                    className={["ib-tg", glyph === "pk" ? "ib-tg-pk" : null].filter(Boolean).join(" ")}
                    aria-hidden="true"
                  >
                    {GLYPH[glyph]}
                  </span>
                );
                return (
                  <th
                    key={col.key}
                    scope="col"
                    style={{ textAlign: align, width: col.width }}
                    title={`${col.header} — ${GLYPH_TITLE[glyph]}`}
                    aria-sort={
                      canSort ? (active ? (sortDir === "asc" ? "ascending" : "descending") : "none") : undefined
                    }
                  >
                    {canSort ? (
                      <button type="button" className="ib-th-btn" onClick={() => toggleSort(col.key)}>
                        {chip}
                        {col.header}
                        {active && <span className="ib-sort-caret">{sortDir === "asc" ? "▲" : "▼"}</span>}
                      </button>
                    ) : (
                      <>
                        {chip}
                        {col.header}
                      </>
                    )}
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody>
            {sortedRows.length === 0 ? (
              <tr>
                <td className="ib-empty" colSpan={columns.length}>
                  {emptyMessage}
                </td>
              </tr>
            ) : (
              sortedRows.map((row, i) => (
                <tr
                  key={rowKey ? rowKey(row, i) : i}
                  className={onRowClick ? "ib-tr-clickable" : undefined}
                  onClick={onRowClick ? () => onRowClick(row, i) : undefined}
                  // F-9: the detail drawers on Drift/Hosts/CollRuns are the
                  // ONLY path to most detail data, driven entirely by this
                  // row click — without a tab stop + key handler that data
                  // is unreachable by keyboard. `tabIndex={-1}` (rather than
                  // omitting the attribute) on a non-clickable row keeps it
                  // explicitly out of the tab order rather than leaving it to
                  // default DOM behavior.
                  tabIndex={onRowClick ? 0 : -1}
                  onKeyDown={
                    onRowClick
                      ? (e) => {
                          if (e.key === "Enter" || e.key === " ") {
                            e.preventDefault();
                            onRowClick(row, i);
                          }
                        }
                      : undefined
                  }
                >
                  {columns.map((col) => {
                    const glyph = col.typeGlyph ?? "text";
                    const align = col.align ?? (glyph === "num" ? "right" : "left");
                    // The NULL contract: whether the cell came from a custom
                    // `render` or the default accessor, a nullish/blank result
                    // becomes the NULL token. Pages cannot forget this.
                    const content = col.render ? col.render(row, i) : defaultCell(readValue(col, row));
                    const body = isNullish(content) ? <NullCell /> : content;
                    return (
                      <td key={col.key} className={align === "right" ? "ib-num" : undefined}>
                        {body}
                      </td>
                    );
                  })}
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      {sb && (
        <div className="ib-statusbar">
          <span className="ib-sb-ok">
            {(sb.shown ?? sortedRows.length).toLocaleString()}
            {sb.total !== undefined ? ` of ${sb.total.toLocaleString()}` : ""} rows
          </span>
          {sb.ms !== undefined && <span>{sb.ms.toLocaleString()} ms</span>}
          <span className="ib-sb-right">
            {sb.note ?? "read-only · no mutations possible from this view"}
          </span>
        </div>
      )}
    </div>
  );
}

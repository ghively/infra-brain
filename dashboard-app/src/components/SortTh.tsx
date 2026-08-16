import type { SortDir } from "../hooks/useSort";

/** A clickable, keyboard-operable column header for use with `useSort`.
 * Sorts the current page of rows only — see the header comment in
 * `hooks/useSort.ts` for why this isn't a global sort. */
export function SortTh<K extends string>({
  label,
  sortKey,
  activeKey,
  dir,
  onSort,
  align,
}: {
  label: string;
  sortKey: K;
  activeKey: K | null;
  dir: SortDir;
  onSort: (key: K) => void;
  align?: "left" | "right";
}) {
  const active = activeKey === sortKey;
  return (
    <th
      className="ib-sort-th"
      role="columnheader"
      aria-sort={active ? (dir === "asc" ? "ascending" : "descending") : "none"}
      tabIndex={0}
      style={{
        padding: "10px 14px",
        textAlign: align ?? "left",
        fontSize: 10,
        fontWeight: 700,
        color: active ? "#a5b4fc" : "#374569",
        textTransform: "uppercase",
        letterSpacing: ".07em",
      }}
      onClick={() => onSort(sortKey)}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onSort(sortKey);
        }
      }}
      title="Sorts the current page only"
    >
      {label}
      {active && <span className="ib-sort-caret">{dir === "asc" ? "▲" : "▼"}</span>}
    </th>
  );
}

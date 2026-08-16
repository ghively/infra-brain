import { useMemo, useState } from "react";

export type SortDir = "asc" | "desc";

/** Generic client-side sort for a table of rows already fetched from the API.
 *
 * MR-I usability nit: none of the ported list pages had sortable columns
 * (the legacy DC shell didn't either). Every list endpoint in this app is
 * server-paginated (limit/offset) with no `sort`/`order` query param, so this
 * hook deliberately only sorts the rows currently on screen — it is NOT a
 * global sort across the full result set. That's called out in the
 * <SortTh> tooltip and in each call site's comment so it doesn't read as a
 * silent undercount the way the legacy page-capped-counts-as-totals bug did
 * (FE-6). If a page ever needs a true global sort, thread `sort`/`order`
 * through to the backend query instead of extending this hook.
 */
export function useSort<T>(rows: T[], initialKey: keyof T | null = null, initialDir: SortDir = "asc") {
  const [sortKey, setSortKey] = useState<keyof T | null>(initialKey);
  const [sortDir, setSortDir] = useState<SortDir>(initialDir);

  function toggle(key: keyof T) {
    if (key === sortKey) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir("asc");
    }
  }

  const sorted = useMemo(() => {
    if (!sortKey) return rows;
    const copy = rows.slice();
    copy.sort((a, b) => {
      const av = a[sortKey];
      const bv = b[sortKey];
      // Nulls/undefined always sort last, regardless of sort direction —
      // do NOT invert this branch for desc, or a whole-array reverse()
      // would flip missing values to the top instead.
      if (av == null && bv == null) return 0;
      if (av == null) return 1;
      if (bv == null) return -1;
      const cmp =
        typeof av === "number" && typeof bv === "number"
          ? av - bv
          : String(av).localeCompare(String(bv), undefined, { numeric: true });
      return sortDir === "desc" ? -cmp : cmp;
    });
    return copy;
  }, [rows, sortKey, sortDir]);

  return { sorted, sortKey, sortDir, toggle };
}

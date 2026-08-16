/** Shared datetime truncation helper (see dashboard-app/DESIGN.md §4).
 *  Truncates an ISO datetime string to "YYYY-MM-DD HH:MM:SS" (space
 *  separator, not "T"; drops fractional seconds and timezone offset).
 *  Returns `undefined` (not a placeholder string like "—" or "N/A") for a
 *  missing value so `DataTable`'s NULL-cell contract renders its own NULL
 *  token — `undefined` means "missing/unknown data", while a literal "—"
 *  means "not applicable"; do not conflate them.
 *
 *  This is the extracted, canonical form of the near-identical `fmtDt`
 *  previously duplicated across McpKeys.tsx, ScanSchedule.tsx, and
 *  Octopushistory.tsx (also AgentConfig.tsx, CollRuns.tsx, Sweeps.tsx). */
export function fmtDt(s?: string | null): string | undefined {
  if (!s) return undefined;
  const d = String(s);
  return d.length >= 19 ? d.slice(0, 10) + " " + d.slice(11, 19) : d;
}

/** Pins fmtDt's truncation + null-handling contract — the shared helper
 *  extracted from McpKeys/ScanSchedule/Octopushistory's near-identical
 *  local copies. Returning `undefined` (not "—"/"N/A") for missing input
 *  is load-bearing: DataTable's NULL-cell contract depends on it (see
 *  DESIGN.md §4). */
import { describe, it, expect } from "vitest";
import { fmtDt } from "./fmtDt";

describe("fmtDt", () => {
  it("truncates a full ISO datetime to 'YYYY-MM-DD HH:MM:SS'", () => {
    expect(fmtDt("2026-07-30T12:34:56.789012+00:00")).toBe("2026-07-30 12:34:56");
    expect(fmtDt("2026-07-30T12:34:56Z")).toBe("2026-07-30 12:34:56");
  });

  it("passes through a short string unchanged", () => {
    expect(fmtDt("2026-07-30")).toBe("2026-07-30");
  });

  it("converts the 'T' separator for an exactly-19-char ISO string (no fractional seconds, no offset)", () => {
    // datetime.isoformat() produces exactly this shape when microsecond == 0
    // on a naive datetime — length is exactly 19, so the boundary check
    // (`>= 19`, not `> 19`) must still apply the space substitution.
    expect(fmtDt("2026-08-04T12:34:56")).toBe("2026-08-04 12:34:56");
  });

  it("returns undefined (not a placeholder) for null/undefined/empty input", () => {
    expect(fmtDt(null)).toBeUndefined();
    expect(fmtDt(undefined)).toBeUndefined();
    expect(fmtDt("")).toBeUndefined();
  });
});

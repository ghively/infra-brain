/** F-11: relTime edge cases. A future timestamp (clock skew between the
 *  collector host and the browser) must never render a negative offset, and
 *  anything over a day must carry a day unit rather than an ever-growing
 *  hour count with no day unit at all. */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { relTime } from "./shared";

describe("relTime", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-08-10T12:00:00Z"));
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("returns the em-dash for a nullish timestamp", () => {
    expect(relTime(null)).toBe("—");
    expect(relTime(undefined)).toBe("—");
  });

  it("renders minutes and hours for recent past timestamps", () => {
    expect(relTime(new Date("2026-08-10T11:55:00Z").toISOString())).toBe("5m ago");
    expect(relTime(new Date("2026-08-10T09:00:00Z").toISOString())).toBe("3h ago");
  });

  it("does not render a negative offset for a future timestamp (clock skew)", () => {
    const future = new Date("2026-08-10T12:03:00Z").toISOString();
    expect(relTime(future)).not.toMatch(/^-/);
  });

  it("renders a day unit once the offset exceeds 24 hours, instead of a triple-digit hour count", () => {
    // 78h ago == 3 days 6 hours ago.
    const threeDaysAgo = new Date("2026-08-07T06:00:00Z").toISOString();
    const result = relTime(threeDaysAgo);
    expect(result).not.toBe("78h ago");
    expect(result).toMatch(/\dd\b|\bday/);
  });
});

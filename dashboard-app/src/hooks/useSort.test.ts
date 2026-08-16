import { act, renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { useSort } from "./useSort";

type Row = { id: number; name: string | null };

describe("useSort", () => {
  it("sorts ascending with nulls last", () => {
    const rows: Row[] = [
      { id: 1, name: "b" },
      { id: 2, name: null },
      { id: 3, name: "a" },
    ];
    const { result } = renderHook(() => useSort(rows, "name", "asc"));

    expect(result.current.sorted.map((r) => r.name)).toEqual(["a", "b", null]);
  });

  it("keeps nulls last after toggling to descending (regression: reverse() must not move nulls to the top)", () => {
    const rows: Row[] = [
      { id: 1, name: "b" },
      { id: 2, name: null },
      { id: 3, name: "a" },
    ];
    const { result } = renderHook(() => useSort(rows, "name", "asc"));

    act(() => {
      result.current.toggle("name");
    });

    expect(result.current.sortDir).toBe("desc");
    // Real values must be in descending order, and the null must still be
    // last, not flipped to the front by a whole-array reverse().
    expect(result.current.sorted.map((r) => r.name)).toEqual(["b", "a", null]);
  });
});

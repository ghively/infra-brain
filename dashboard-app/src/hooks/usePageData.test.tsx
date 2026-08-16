import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { usePageData } from "./usePageData";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("usePageData", () => {
  it("starts with data=null and loading=true, then populates data on success", async () => {
    const fetcher = vi.fn().mockResolvedValue({ items: [1, 2, 3] });
    const { result } = renderHook(() => usePageData(fetcher, []));

    expect(result.current.data).toBeNull();
    expect(result.current.loading).toBe(true);

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.data).toEqual({ items: [1, 2, 3] });
    expect(result.current.error).toBeNull();
  });

  it("sets error and clears loading on a first-load failure, data stays null", async () => {
    const fetcher = vi.fn().mockRejectedValue(new Error("boom"));
    const { result } = renderHook(() => usePageData(fetcher, []));

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.data).toBeNull();
    expect(result.current.error).toBe("Error: boom");
  });

  it("a reload failure AFTER a successful first load keeps the stale data, does not null it out", async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce({ items: [1] })
      .mockRejectedValueOnce(new Error("transient blip"));
    const { result } = renderHook(() => usePageData(fetcher, []));

    await waitFor(() => expect(result.current.data).toEqual({ items: [1] }));

    await act(async () => {
      await result.current.reload();
    });

    expect(result.current.data).toEqual({ items: [1] }); // NOT wiped
    expect(result.current.error).toBe("Error: transient blip");
    expect(result.current.loading).toBe(false); // never re-enters full loading state
  });

  it("a reload success after a prior error clears the error", async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce({ items: [1] })
      .mockRejectedValueOnce(new Error("blip"))
      .mockResolvedValueOnce({ items: [1, 2] });
    const { result } = renderHook(() => usePageData(fetcher, []));

    await waitFor(() => expect(result.current.data).toEqual({ items: [1] }));
    await act(async () => {
      await result.current.reload();
    });
    expect(result.current.error).toBe("Error: blip");

    await act(async () => {
      await result.current.reload();
    });
    expect(result.current.error).toBeNull();
    expect(result.current.data).toEqual({ items: [1, 2] });
  });

  it("re-fetches when deps change", async () => {
    const fetcher = vi.fn().mockResolvedValue({ ok: true });
    let dep = "open";
    const { rerender } = renderHook(() => usePageData(fetcher, [dep]));

    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1));

    dep = "closed";
    rerender();
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(2));
  });

  it("does not let a slower earlier request overwrite a faster later request (out-of-order responses)", async () => {
    // Simulates a filter-driven reload: request A (issued for the OLD filter
    // value) is slow; request B (issued right after for the NEW filter
    // value) is fast and resolves first. A's stale result must never
    // clobber B's already-rendered, correct data.
    let resolveA: (v: { forFilter: string }) => void;
    const pendingA = new Promise<{ forFilter: string }>((resolve) => {
      resolveA = resolve;
    });
    const fetcher = vi
      .fn()
      .mockImplementationOnce(() => pendingA) // request A: never resolves until we say so
      .mockResolvedValueOnce({ forFilter: "closed" }); // request B: resolves immediately

    let dep = "open";
    const { result, rerender } = renderHook(() => usePageData(fetcher, [dep]));

    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1)); // request A in flight

    // Filter changes before A resolves -> issues request B.
    dep = "closed";
    rerender();
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(2));

    // B (faster, newer) resolves and renders first.
    await waitFor(() =>
      expect(result.current.data).toEqual({ forFilter: "closed" }),
    );

    // A (slower, older) resolves after B already rendered. It must be
    // dropped, not overwrite the screen with stale data.
    await act(async () => {
      resolveA({ forFilter: "open" });
      await pendingA;
    });

    expect(result.current.data).toEqual({ forFilter: "closed" });
  });
});

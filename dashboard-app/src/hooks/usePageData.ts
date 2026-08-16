/** Shared data-fetching hook (#91 — frontend error-handling redesign).
 *
 *  Replaces the 34-page `if (error) return <div role="alert">...</div>`
 *  pattern, which unmounts the ENTIRE page (filters, tabs, any already-
 *  loaded data) on any fetch error, including a transient blip during a
 *  filter-driven reload (which — unlike the 5-minute background
 *  auto-refresh — was never wrapped in runQuiet()).
 *
 *  Design (see the plan doc's Task 2 for the full rationale):
 *    - `data` is populated on the first success and NEVER reset to null by
 *      a later error — only ever replaced by a later success.
 *    - The very first load surfaces its error immediately (not quiet) —
 *      matching today's "nothing loaded, something's wrong" behavior.
 *    - Every load AFTER the first (i.e. every dep-change-triggered reload,
 *      including filter changes) runs through runQuiet() — the same
 *      suppression useAutoRefresh already applies to the 5-minute
 *      background refresh — so a transient blip while a user changes a
 *      filter does not flash the global banners or wipe the view.
 *    - `loading` is true only while `data` is still null (i.e. only before
 *      the very first successful load) — a filter change while data
 *      already exists never re-enters a full loading state.
 *
 *  A page still owns its OWN filter state (status/domain/window/etc.) and
 *  passes a fetcher callback + that filter state as `deps`; this hook does
 *  not know or care what the filters are, mirroring how pages already build
 *  their own `load` callback today (see Drift.tsx's existing `load`).
 */
import { useCallback, useEffect, useRef, useState } from "react";

import { AuthRequired, redirectToLogin, runQuiet } from "../api";

export type UsePageDataResult<T> = {
  data: T | null;
  error: string | null;
  loading: boolean;
  reload: () => Promise<void>;
};

export function usePageData<T>(
  fetcher: () => Promise<T>,
  deps: unknown[],
): UsePageDataResult<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const hasLoadedOnce = useRef(false);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;
  // Monotonic request counter so a slower, earlier-issued fetch can't
  // overwrite the screen after a faster, later-issued fetch has already
  // rendered (classic out-of-order-response race — see dep-change reloads,
  // e.g. rapid filter changes).
  const requestIdRef = useRef(0);

  const runLoad = useCallback(async (quiet: boolean) => {
    const requestId = ++requestIdRef.current;
    const isStale = () => requestIdRef.current !== requestId;
    const doFetch = async () => {
      try {
        const result = await fetcherRef.current();
        if (isStale()) return; // superseded by a newer request; drop this result
        setData(result);
        setError(null);
      } catch (e) {
        if (e instanceof AuthRequired) {
          redirectToLogin();
          return;
        }
        if (isStale()) return; // superseded by a newer request; drop this result
        setError(String(e));
        // Deliberately NOT clearing `data` here — stale data stays on
        // screen; the GlobalErrorBanner/EndpointErrorBanner (already wired
        // into api.ts's fetchErrors store) carry the "something's wrong"
        // signal instead of an unmounted page.
      } finally {
        hasLoadedOnce.current = true;
      }
    };
    if (quiet) {
      await runQuiet(doFetch);
    } else {
      await doFetch();
    }
  }, []);

  const reload = useCallback(async () => {
    await runLoad(hasLoadedOnce.current);
  }, [runLoad]);

  // eslint-disable-next-line react-hooks/exhaustive-deps -- deps is caller-controlled, mirroring every existing page's own `useCallback(load, [status, domain, ...])` pattern.
  useEffect(() => {
    void runLoad(hasLoadedOnce.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  return {
    data,
    error,
    loading: data === null && !hasLoadedOnce.current,
    reload,
  };
}

/** Auto-refresh primitives for the Wave 6.1 Vite+React dashboard (DR-6.1).
 *  Port of v1 index.html's single 60s tick timer (L3824-3830) and its 5-minute
 *  quiet background re-fetch (`_refreshCore`, L4394-4426).
 *
 *  v1 behavior reproduced here:
 *    - ONE 60s interval. Every tick bumps a monotonic counter used only to force
 *      re-render of relative "Xm ago" timestamps.
 *    - Every 5th tick (5 min) a QUIET re-fetch runs: failures do NOT surface the
 *      error banners and stale data stays put (no error flash).
 *    - Overlapping runs are skipped; the whole mechanism is disabled when the
 *      `?demo` query param is present.
 *
 *  ── Contract for the pages agent (B2) ─────────────────────────────────────
 *  Wrap the shell once in <RefreshProvider> (done in App.tsx). Then, in any
 *  page/component, drop your EXISTING loader into useAutoRefresh:
 *
 *      const load = useCallback(async () => { ...apiGet(...)... }, [deps]);
 *      useEffect(() => { load(); }, [load]);   // initial load, as today
 *      useAutoRefresh(load);                   // + auto every 5 min, quiet
 *
 *  You do NOT change your loader. useAutoRefresh runs it under runQuiet(), so
 *  the ordinary apiGet calls inside it become quiet for the duration — a
 *  transient failure will not flash the banners and will not clear existing
 *  ones. Errors are swallowed EXCEPT AuthRequired, which still redirects to
 *  login. Overlapping runs are skipped and `?demo` disables it.
 *
 *  For components that only render relative timestamps and need no re-fetch,
 *  read the tick directly: `const tick = useRefreshTick();` — referencing it in
 *  render is enough to re-render every 60s.
 */
import {
  createContext,
  createElement,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { AuthRequired, redirectToLogin, runQuiet } from "../api";

const isDemo = (): boolean => new URLSearchParams(window.location.search).has("demo");

const RefreshTickContext = createContext<number>(0);

/** Ticks a monotonic counter every 60s (disabled under `?demo`) and provides it
 *  to descendants via context. Mount once, wrapping the app shell. */
export function RefreshProvider({ children }: { children: ReactNode }): ReactNode {
  const [tick, setTick] = useState(0);
  useEffect(() => {
    if (isDemo()) return;
    const id = setInterval(() => setTick((t) => t + 1), 60000);
    return () => clearInterval(id);
  }, []);
  return createElement(RefreshTickContext.Provider, { value: tick }, children);
}

/** The current monotonic 60s tick. Consume it in render to re-render relative
 *  ("Xm ago") timestamps every minute. */
export function useRefreshTick(): number {
  return useContext(RefreshTickContext);
}

export type UseAutoRefreshOptions = {
  /** Run the loader every Nth tick. Default 5 (5 minutes), matching v1. */
  everyTicks?: number;
};

/** Re-run `loader` every Nth tick (default every 5th → 5 min) in QUIET mode.
 *  Errors are swallowed (stale data stays, no banner) except AuthRequired,
 *  which redirects to login. Overlapping runs are skipped; no-op under `?demo`. */
export function useAutoRefresh(loader: () => Promise<void>, opts?: UseAutoRefreshOptions): void {
  const everyTicks = opts?.everyTicks ?? 5;
  const tick = useRefreshTick();
  const runningRef = useRef(false);
  const loaderRef = useRef(loader);
  loaderRef.current = loader;

  useEffect(() => {
    if (tick === 0) return; // initial mount — page does its own first load
    if (tick % everyTicks !== 0) return; // only every Nth tick
    if (isDemo()) return;
    if (runningRef.current) return; // overlap guard

    runningRef.current = true;
    void (async () => {
      try {
        await runQuiet(() => loaderRef.current());
      } catch (e) {
        if (e instanceof AuthRequired) {
          redirectToLogin();
          return;
        }
        // QUIET: swallow any other error — stale data stays, no banner flash.
      } finally {
        runningRef.current = false;
      }
    })();
  }, [tick, everyTicks]);
}

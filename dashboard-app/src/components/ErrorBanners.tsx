import { useSyncExternalStore } from "react";
import { useLocation } from "react-router-dom";
import { fetchErrors } from "../api";

/** Two-layer self-healing fetch-error banners for the Wave 6.1 dashboard
 *  (DR-6.1). Port of v1 index.html `_showLoadBanner` (L3896-3914, top/global)
 *  and `_renderFetchErrBanner` (L3916-3938, bottom/per-endpoint).
 *
 *  DEVIATION FROM V1 (intentional modernization): v1 imperatively created and
 *  removed <div>s on document.body. v2 is React, so both banners are declarative
 *  components subscribing to the `fetchErrors` store via useSyncExternalStore;
 *  React mounts/unmounts them as the store fills and self-heals. Behavior and
 *  styling (colors, fixed positioning, self-healing) match v1.
 *
 *  Both banners are suppressed on the /login route. In v1 they were suppressed
 *  whenever the auth gate (`#ib-auth-gate` / `_authGateShown`) was up; in v2 the
 *  401 path redirects to /login instead of overlaying a gate, and AuthRequired
 *  is never noted into the store — so the practical, equivalent rule is: never
 *  show these banners on the login route, and (structurally) never as a reaction
 *  to AuthRequired. */

// v1 banner palette (index.html L3908 / L3932) — kept identical.
const BANNER_BG = "#3d1d1d";
const BANNER_FG = "#f85149";
const BANNER_EDGE = "rgba(248,81,73,.3)";

function useFetchErrors(): ReadonlyArray<readonly [string, string]> {
  return useSyncExternalStore(fetchErrors.subscribe, fetchErrors.getAll, fetchErrors.getAll);
}

function useSuppressed(): boolean {
  return useLocation().pathname === "/login";
}

/** Layer A (global, top): shown whenever any live-data fetch is currently
 *  failing. Generic reassurance that empty results are real, not sample data. */
export function GlobalErrorBanner(): React.ReactNode {
  const errs = useFetchErrors();
  const suppressed = useSuppressed();
  if (suppressed || errs.length === 0) return null;
  const n = errs.length;
  return (
    <div
      role="alert"
      style={{
        position: "fixed",
        top: 0,
        left: 0,
        right: 0,
        zIndex: 9998,
        background: BANNER_BG,
        color: BANNER_FG,
        borderBottom: `1px solid ${BANNER_EDGE}`,
        padding: "8px 16px",
        fontSize: 13,
        textAlign: "center",
      }}
    >
      {`⚠ Could not load live data for ${n} view${n > 1 ? "s" : ""} — the backend may be unreachable. Showing empty results, not sample data.`}
    </div>
  );
}

/** Layer B (per-endpoint, bottom): lists the currently-failing endpoints and
 *  removes itself when the store empties (self-healing — a later success on a
 *  path clears it via api.ts request()). */
export function EndpointErrorBanner(): React.ReactNode {
  const errs = useFetchErrors();
  const suppressed = useSuppressed();
  if (suppressed || errs.length === 0) return null;
  const parts = errs.slice(0, 4).map(([p, m]) => `${p.replace(/^\/api\//, "")} — ${m}`);
  const extra = errs.length > 4 ? ` (+${errs.length - 4} more)` : "";
  return (
    <div
      role="status"
      style={{
        position: "fixed",
        bottom: 0,
        left: 0,
        right: 0,
        zIndex: 9998,
        background: BANNER_BG,
        color: BANNER_FG,
        borderTop: `1px solid ${BANNER_EDGE}`,
        padding: "6px 16px",
        fontSize: 12,
        textAlign: "center",
      }}
    >
      {`⚠ ${errs.length} request${errs.length > 1 ? "s" : ""} failing: ${parts.join(" · ")}${extra}`}
    </div>
  );
}

import { useEffect, useState, type ReactNode } from "react";
import { apiGet, AuthRequired, redirectToLogin } from "./api";

/** GET /api/dashboard/me — current session identity (schemas.py). Canonical
 *  definition; Sidebar.tsx re-exports/imports this rather than keeping its
 *  own copy (see AuthGate below for why the two are now linked). */
export type Me = {
  authenticated: boolean;
  dev_mode: boolean;
  username: string | null;
  name: string | null;
  role: string | null;
};

type GateState = { status: "checking" } | { status: "ready"; me: Me | null };

/** TRK-236 fix: gates the authenticated app shell (Sidebar/Topbar/routed
 *  pages, see App.tsx's AuthenticatedLayout) behind a single session check
 *  before any of it mounts.
 *
 *  Root cause this fixes: previously App.tsx mounted Sidebar, Topbar and the
 *  matched page unconditionally on every route (including /login, per the
 *  old comment on redirectToLogin() in api.ts). Each of those independently
 *  fires its own protected fetches on mount with no auth gate at all, so an
 *  unauthenticated visit produced ~18 wasted 401s in parallel (counts x3,
 *  scan_points x2, system_health x2, collection_runs x2, plus
 *  graph/relationships, drift_events, activity, resources, notifications,
 *  me, version...) — all racing to be the one whose AuthRequired handler
 *  calls redirectToLogin() first. The redirect always eventually won (login
 *  still rendered correctly), but only after every other component's mount
 *  effect had already fired its doomed request.
 *
 *  Fix: perform ONE GET /api/dashboard/me check here, before the shell
 *  (Sidebar/Topbar/page) mounts at all. On 401 (AuthRequired), redirect
 *  immediately and never mount the shell — none of its child fetches fire.
 *  On success, mount the shell and hand the already-fetched `me` down to
 *  Sidebar so it does not re-fetch /me itself (no added latency vs. today:
 *  this is the same round trip Sidebar already made, just moved earlier).
 *  On any OTHER error (network blip, 500), fail OPEN and mount the shell
 *  anyway — matching every other per-component catch in this app (never
 *  block the whole shell on a transient identity-fetch failure).
 *
 *  F-4: GET /api/dashboard/me has NO auth dependency — for an anonymous
 *  request it returns 200 `{authenticated:false,...}`, not a 401. The
 *  original fix above only ever checked for the AuthRequired (401) *throw*,
 *  so an unauthenticated visit resolved this successful 200 into `status:
 *  "ready"` and mounted the shell anyway — every doomed 401 fetch this gate
 *  exists to prevent still fired, with login reached only via one child's
 *  redirect race, i.e. exactly the pre-fix behaviour. Treating a resolved
 *  `authenticated === false` the same as a thrown AuthRequired closes that
 *  gap: both redirect immediately and never mount children.
 *
 *  /login itself is NOT wrapped in this gate (see App.tsx) — it needs no
 *  session and must never fire session-gated fetches waiting on one. Nor is
 *  `/views/:token` (F-2, ViewShare.tsx) — a public share link must render for
 *  an anonymous viewer, which this gate (now correctly redirecting on
 *  `authenticated:false`) would otherwise block. */
export function AuthGate({ children }: { children: (me: Me | null) => ReactNode }) {
  const [state, setState] = useState<GateState>({ status: "checking" });

  useEffect(() => {
    let cancelled = false;
    apiGet<Me>("/api/dashboard/me")
      .then((me) => {
        if (cancelled) return;
        if (!me.authenticated) {
          // F-4: a 200 response with authenticated:false is anonymous, not
          // an error — the pre-fix bug was treating this as "ready" and
          // mounting the shell. Redirect exactly as the AuthRequired (401)
          // branch below does, and stay in "checking" so children never mount.
          redirectToLogin();
          return;
        }
        setState({ status: "ready", me });
      })
      .catch((e) => {
        if (cancelled) return;
        if (e instanceof AuthRequired) {
          redirectToLogin();
          return; // stay in "checking" — navigation is already underway,
          // so the shell (and its fetches) never mounts at all.
        }
        setState({ status: "ready", me: null });
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Blank while checking — for an already-valid session this is the same
  // single /me round trip that used to happen after the shell mounted, so
  // there is no NEW latency, just no shell-then-blank-then-populate flash.
  if (state.status === "checking") return null;
  return <>{children(state.me)}</>;
}

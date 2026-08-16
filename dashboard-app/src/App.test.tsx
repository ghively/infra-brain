/** TRK-236 integration coverage: the whole App tree, not just AuthGate in
 *  isolation (see AuthGate.test.tsx for the unit-level contract).
 *
 *  Before the fix, App.tsx mounted Sidebar, Topbar and the routed page
 *  unconditionally regardless of auth state. Each of those fires its own
 *  protected fetch(es) on mount, so an unauthenticated visit produced ~18
 *  wasted 401s in parallel (counts x3, scan_points x2, system_health x2,
 *  collection_runs x2, plus graph/relationships, drift_events, activity,
 *  resources, notifications, me, version) before redirectToLogin() finally
 *  navigated away. These tests assert the actual fix at the tree level: an
 *  unauthenticated visit fires exactly one request (the gate's own /me
 *  check) and nothing else, the login page never even attempts the gate's
 *  check, and an authenticated visit still loads normally — including that
 *  /me itself is fetched only once (Sidebar reuses AuthGate's result rather
 *  than re-fetching it), not duplicated by moving the check earlier.
 */
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import * as api from "./api";
import App from "./App";

let originalLocation: Location;

beforeEach(() => {
  originalLocation = window.location;
  // The embedded sidebar chat panel calls scrollIntoView on mount/update;
  // jsdom doesn't implement it (see Sidebar.test.tsx for the same stub).
  Element.prototype.scrollIntoView = vi.fn();
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  window.history.pushState({}, "", "/dashboard2/");
  Object.defineProperty(window, "location", {
    value: originalLocation,
    writable: true,
    configurable: true,
  });
});

function mockLocationAssign(pathname: string): ReturnType<typeof vi.fn> {
  const assignSpy = vi.fn();
  Object.defineProperty(window, "location", {
    value: { ...originalLocation, pathname, assign: assignSpy },
    writable: true,
    configurable: true,
  });
  return assignSpy;
}

const BASE_COUNTS = {
  open_drift: 7,
  open_cves: 3,
  eol_overdue: 2,
  eol_total: 5,
  invrec_proposed: 1,
  invrec_total: 4,
  total_resources: 42,
  applied_instincts: 6,
};

const ME = { authenticated: true, dev_mode: false, username: "operator", name: "the maintainer", role: "admin" };
const VERSION = { version: "1.0.0", environment: "dev" };

/** Full happy-path stub covering every endpoint Home + Sidebar + Topbar fire
 *  on mount, so an authenticated render of the whole tree resolves cleanly. */
function mockAuthenticatedTree() {
  return vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
    if (url.includes("/counts")) return BASE_COUNTS;
    if (url.includes("/me")) return ME;
    if (url.includes("/version")) return VERSION;
    if (url.includes("/system_health")) return { items: [], total: 0 };
    if (url.includes("/collection_runs")) return { items: [], total: 0 };
    if (url.includes("/scan_points")) return { items: [], total: 0 };
    if (url.includes("/drift_events")) return { items: [], total: 0 };
    if (url.includes("/activity")) return { items: [], total: 0 };
    if (url.includes("/resources")) return { items: [], total: 0 };
    if (url.includes("/notifications")) return { items: [], total: 0 };
    if (url.includes("/relationships")) return { items: [], total: 0 };
    return { items: [], total: 0 };
  });
}

describe("App — TRK-236 auth-gated mount", () => {
  it("unauthenticated visit to the dashboard fires exactly one request (the gate's /me check) and redirects, never mounting Sidebar/Topbar/Home fetches", async () => {
    window.history.pushState({}, "", "/dashboard2/");
    const assignSpy = mockLocationAssign("/dashboard2/");
    const getSpy = vi.spyOn(api, "apiGet").mockRejectedValue(new api.AuthRequired());

    render(<App />);

    await waitFor(() => expect(assignSpy).toHaveBeenCalled());

    // The old bug: ~18 calls (counts x3, scan_points x2, system_health x2,
    // etc.) all firing before the redirect settled. The fix: exactly one.
    expect(getSpy).toHaveBeenCalledTimes(1);
    expect(getSpy).toHaveBeenCalledWith("/api/dashboard/me");
    expect(assignSpy.mock.calls[0][0]).toContain("/dashboard2/login");

    // Sidebar's "Sign out" button (and thus the whole nav shell) never mounted.
    expect(screen.queryByRole("button", { name: "Sign out" })).toBeNull();
  });

  it("the login page itself fires no protected fetches and needs no auth check", async () => {
    window.history.pushState({}, "", "/dashboard2/login");
    const assignSpy = mockLocationAssign("/dashboard2/login");
    const getSpy = vi.spyOn(api, "apiGet").mockRejectedValue(new api.AuthRequired());

    render(<App />);

    await screen.findByRole("button", { name: /sign in/i });

    // Login renders standalone — no gate check, no Sidebar/Topbar fetches.
    expect(getSpy).not.toHaveBeenCalled();
    expect(assignSpy).not.toHaveBeenCalled();
  });

  it("an already-authenticated visit loads the dashboard normally: /me is fetched only once (not duplicated by Sidebar) and the page's own data still loads", async () => {
    window.history.pushState({}, "", "/dashboard2/");
    const assignSpy = mockLocationAssign("/dashboard2/");
    const getSpy = mockAuthenticatedTree();

    render(<App />);

    // Home's data renders once counts resolve.
    await waitFor(() => expect(screen.getByRole("button", { name: "Sign out" })).toBeTruthy());
    await waitFor(() => {
      const meCalls = getSpy.mock.calls.filter(([url]) => url === "/api/dashboard/me");
      expect(meCalls.length).toBe(1);
    });

    expect(assignSpy).not.toHaveBeenCalled();
    // The page's own data-fetching still ran normally post-gate.
    expect(getSpy).toHaveBeenCalledWith(expect.stringContaining("/counts"));
    expect(getSpy).toHaveBeenCalledWith(expect.stringContaining("/drift_events"));
  });
});

/** F-2 + F-4 interaction coverage: F-4 makes AuthGate strictly redirect on
 *  `authenticated:false` (not just on a thrown 401), which risked also
 *  blocking the public `/views/:token` share route (F-2) for its whole
 *  intended anonymous audience. F-2's fix — moving that route outside
 *  AuthenticatedLayout entirely, as a sibling of /login — means it never hits
 *  AuthGate at all, so the two fixes don't fight each other. These tests
 *  assert BOTH halves of that story at the full App-tree level (see
 *  AuthGate.test.tsx for the F-4 unit-level contract in isolation, and
 *  ViewShare.test.tsx for that page's own fetch/render contract). */
describe("App — F-2/F-4: public share links stay reachable by an anonymous viewer after AuthGate is made stricter", () => {
  const SHARED_VIEW = {
    id: "v1",
    title: "Fleet Overview",
    prompt: "show me the fleet",
    openui_lang: "text: hello",
    is_public: true,
    created_at: "2026-07-01T00:00:00Z",
  };

  it("F-2: an anonymous visit to /views/:token renders the public view directly — no /me call, no redirect, no Sidebar/Topbar", async () => {
    window.history.pushState({}, "", "/dashboard2/views/tok123");
    const assignSpy = mockLocationAssign("/dashboard2/views/tok123");
    // Anything other than this view's own fetch throws AuthRequired — if the
    // route were still under AuthGate/Sidebar/Topbar (the pre-fix shape),
    // any one of their session-gated fetches would redirect this away from
    // the shared view before it ever rendered.
    const getSpy = vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
      if (url.includes("/views/tok123")) return SHARED_VIEW;
      throw new api.AuthRequired();
    });

    render(<App />);

    await waitFor(() => expect(screen.getByText("Fleet Overview")).toBeInTheDocument());

    expect(getSpy).not.toHaveBeenCalledWith("/api/dashboard/me");
    expect(getSpy).toHaveBeenCalledTimes(1);
    expect(assignSpy).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "Sign out" })).toBeNull();
  });

  it("F-4: an unauthenticated visit to a normal (non-share) route still redirects when /me resolves 200 authenticated:false, rather than mounting the shell", async () => {
    window.history.pushState({}, "", "/dashboard2/");
    const assignSpy = mockLocationAssign("/dashboard2/");
    const getSpy = vi.spyOn(api, "apiGet").mockResolvedValue({
      authenticated: false,
      dev_mode: false,
      username: null,
      name: null,
      role: null,
    });

    render(<App />);

    await waitFor(() => expect(assignSpy).toHaveBeenCalled());
    expect(getSpy).toHaveBeenCalledTimes(1);
    expect(assignSpy.mock.calls[0][0]).toContain("/dashboard2/login");
    expect(screen.queryByRole("button", { name: "Sign out" })).toBeNull();
  });
});

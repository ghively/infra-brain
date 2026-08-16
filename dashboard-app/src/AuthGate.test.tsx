/** TRK-236 regression coverage for AuthGate in isolation.
 *
 *  Root cause: App.tsx used to mount Sidebar, Topbar and the routed page
 *  unconditionally on every route (including /login), each firing its own
 *  protected fetch(es) on mount with no auth gate at all — an unauthenticated
 *  visit fired ~18 doomed 401s (counts x3, scan_points x2, system_health x2,
 *  collection_runs x2, plus graph/relationships, drift_events, activity,
 *  resources, notifications, me, version...) before redirectToLogin() finally
 *  navigated away. These tests pin AuthGate's contract directly: exactly one
 *  GET /api/dashboard/me before anything renders, redirect-and-never-render
 *  on 401, and fail-open (render anyway) on any other error. Sidebar.test.tsx
 *  and App.test.tsx cover the same fix at the component-integration level.
 */
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import * as api from "./api";
import { AuthGate } from "./AuthGate";

let originalLocation: Location;

beforeEach(() => {
  originalLocation = window.location;
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  Object.defineProperty(window, "location", {
    value: originalLocation,
    writable: true,
    configurable: true,
  });
});

function mockLocationAssign(pathname = "/dashboard2/hosts"): ReturnType<typeof vi.fn> {
  const assignSpy = vi.fn();
  Object.defineProperty(window, "location", {
    value: { ...originalLocation, pathname, assign: assignSpy },
    writable: true,
    configurable: true,
  });
  return assignSpy;
}

describe("AuthGate", () => {
  it("renders nothing and redirects to login on a 401, without ever rendering children", async () => {
    const assignSpy = mockLocationAssign();
    const getSpy = vi.spyOn(api, "apiGet").mockRejectedValue(new api.AuthRequired());

    const { container } = render(
      <AuthGate>{() => <div>protected shell content</div>}</AuthGate>,
    );

    await waitFor(() => expect(assignSpy).toHaveBeenCalled());

    // Exactly one call — the gate's own /me check — regardless of how many
    // downstream components WOULD have fired their own fetches had children
    // ever mounted.
    expect(getSpy).toHaveBeenCalledTimes(1);
    expect(getSpy).toHaveBeenCalledWith("/api/dashboard/me");
    expect(assignSpy.mock.calls[0][0]).toContain("/dashboard2/login");
    expect(screen.queryByText("protected shell content")).toBeNull();
    expect(container.textContent).toBe("");
  });

  it("F-4: redirects on a 200 response with authenticated:false, without ever rendering children", async () => {
    // GET /api/dashboard/me has no auth dependency (dashboard_auth.py) — an
    // anonymous request resolves 200 {authenticated:false,...}, it never
    // throws AuthRequired. The pre-fix bug: this resolved into `status:
    // "ready"`, mounting the shell (and every doomed session-gated fetch
    // inside it) before some child's own 401 eventually redirected.
    const assignSpy = mockLocationAssign();
    const getSpy = vi.spyOn(api, "apiGet").mockResolvedValue({
      authenticated: false,
      dev_mode: false,
      username: null,
      name: null,
      role: null,
    });

    render(<AuthGate>{() => <div>protected shell content</div>}</AuthGate>);

    await waitFor(() => expect(assignSpy).toHaveBeenCalled());

    expect(getSpy).toHaveBeenCalledTimes(1);
    expect(assignSpy.mock.calls[0][0]).toContain("/dashboard2/login");
    expect(screen.queryByText("protected shell content")).toBeNull();
  });

  it("renders children with the resolved identity on success, with no redirect", async () => {
    const assignSpy = mockLocationAssign();
    const me = { authenticated: true, dev_mode: false, username: "operator", name: "the maintainer", role: "admin" };
    vi.spyOn(api, "apiGet").mockResolvedValue(me);

    render(<AuthGate>{(resolved) => <div>hello {resolved?.username}</div>}</AuthGate>);

    await screen.findByText("hello operator");
    expect(assignSpy).not.toHaveBeenCalled();
  });

  it("fails open (renders children) on a non-auth error rather than blocking the shell forever", async () => {
    const assignSpy = mockLocationAssign();
    vi.spyOn(api, "apiGet").mockRejectedValue(new Error("network unreachable"));

    render(<AuthGate>{(resolved) => <div>shell up, me={String(resolved)}</div>}</AuthGate>);

    await screen.findByText("shell up, me=null");
    expect(assignSpy).not.toHaveBeenCalled();
  });
});

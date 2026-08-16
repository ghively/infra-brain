import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { redirectToLogin } from "./api";

/** Regression coverage for the infinite-redirect-loop bug: Sidebar/Topbar
 *  render on every route (including /login, App.tsx) and each fire their own
 *  protected API calls on mount. Without the no-op guard in redirectToLogin,
 *  visiting /login while unauthenticated triggered their calls too, each 401
 *  recomputed `next=` from the CURRENT (already /login?next=...) URL, and the
 *  resulting redirect nested another layer of encoding on top of the last —
 *  reported live as rapid page-flashing ending in a 400 from an exponentially
 *  long URL. */
describe("redirectToLogin", () => {
  let assignSpy: ReturnType<typeof vi.fn>;
  let originalLocation: Location;

  beforeEach(() => {
    originalLocation = window.location;
    assignSpy = vi.fn();
    // window.location's setter type is `string` (location = "url" shorthand),
    // so plain assignment doesn't accept a Location-shaped object even with a
    // cast; defineProperty sidesteps the setter's type entirely.
    Object.defineProperty(window, "location", {
      value: { ...originalLocation, assign: assignSpy },
      writable: true,
      configurable: true,
    });
  });

  afterEach(() => {
    Object.defineProperty(window, "location", {
      value: originalLocation,
      writable: true,
      configurable: true,
    });
  });

  it("redirects to /dashboard2/login with an encoded next= when not already on the login page", () => {
    window.location.pathname = "/dashboard2/hosts";
    window.location.search = "?foo=bar";
    window.location.hash = "";

    redirectToLogin();

    expect(assignSpy).toHaveBeenCalledWith(
      "/dashboard2/login?next=" + encodeURIComponent("/dashboard2/hosts?foo=bar"),
    );
  });

  it("is a no-op when already on /dashboard2/login", () => {
    window.location.pathname = "/dashboard2/login";
    window.location.search = "?next=%2Fdashboard2%2Fhosts";

    redirectToLogin();

    expect(assignSpy).not.toHaveBeenCalled();
  });

  it("is a no-op even with an already-nested next= (exact shape of the reported bug)", () => {
    window.location.pathname = "/dashboard2/login";
    window.location.search =
      "?next=" + encodeURIComponent("/dashboard2/login?next=%2Fdashboard2%2Flogin");

    redirectToLogin();

    expect(assignSpy).not.toHaveBeenCalled();
  });
});

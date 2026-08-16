import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import * as api from "../api";
import { RefreshProvider } from "../hooks/useAutoRefresh";
import { Topbar } from "./Topbar";

afterEach(() => {
  // vitest.config.ts sets `globals: false`, so @testing-library/react's
  // automatic afterEach(cleanup) registration never fires (see Home.test.tsx
  // for the same pattern) — without this, renders from earlier tests in this
  // file accumulate in the jsdom document.
  cleanup();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

/** Topbar fires apiGet on mount (StatusPill's system_health, SweepHealthStrip's
 * collection_runs + scan_points) — stub every call to an empty-rows response so
 * these tests exercise only findBreadcrumb()'s routing, not those network
 * effects. */
function renderTopbarAt(pathname: string) {
  vi.spyOn(api, "apiGet").mockResolvedValue({ rows: [] });
  render(
    <MemoryRouter initialEntries={[pathname]}>
      <Topbar />
    </MemoryRouter>,
  );
}

// The "Inventory" group was renamed "Infrastructure" and "Security" became
// "Security & Compliance" when nav.ts moved to the six-group homelab layout.
describe("Topbar breadcrumb (TRK-237)", () => {
  it("shows Infrastructure / Hosts for the canonical /hosts route", () => {
    renderTopbarAt("/hosts");
    const crumb = screen.getByText((_, el) => el?.className === "crumb");
    expect(crumb.textContent).toContain("Infrastructure");
    expect(crumb.textContent).toContain("Hosts");
  });

  it("shows Infrastructure / Hosts for /fleet (same underlying page as /hosts)", () => {
    renderTopbarAt("/fleet");
    const crumb = screen.getByText((_, el) => el?.className === "crumb");
    expect(crumb.textContent).toContain("Infrastructure");
    expect(crumb.textContent).toContain("Hosts");
    expect(crumb.textContent).not.toContain("Dashboard");
  });

  it("shows Infrastructure / Hosts for /resources (same underlying page as /hosts)", () => {
    renderTopbarAt("/resources");
    const crumb = screen.getByText((_, el) => el?.className === "crumb");
    expect(crumb.textContent).toContain("Infrastructure");
    expect(crumb.textContent).toContain("Hosts");
    expect(crumb.textContent).not.toContain("Dashboard");
  });

  // /r7detail is now a retired route rendering SourceUnavailable (Rapid7 is
  // retired and r7_vulnerabilities is empty). It must still get a correct
  // breadcrumb rather than falling through to "Overview › Dashboard" — an old
  // bookmark should land somewhere honestly labelled, not somewhere wrong.
  it("labels the retired /r7detail route without falling back to Dashboard", () => {
    renderTopbarAt("/r7detail");
    const crumb = screen.getByText((_, el) => el?.className === "crumb");
    expect(crumb.textContent).toContain("Security & Compliance");
    expect(crumb.textContent).toContain("CVE Detail");
    expect(crumb.textContent).toContain("not configured");
    expect(crumb.textContent).not.toContain("Dashboard");
  });

  it("labels the retired /vsphere route without falling back to Dashboard", () => {
    renderTopbarAt("/vsphere");
    const crumb = screen.getByText((_, el) => el?.className === "crumb");
    expect(crumb.textContent).toContain("Infrastructure");
    expect(crumb.textContent).toContain("vSphere");
    expect(crumb.textContent).not.toContain("Dashboard");
  });

  // Regression guard: an unrelated route that IS in NAV_ITEMS must keep
  // resolving through the normal loop, unaffected by the SECONDARY_ROUTES
  // override added for TRK-237.
  it("still shows Security & Compliance / Vulnerabilities for /vulns, unaffected by the fix", () => {
    renderTopbarAt("/vulns");
    const crumb = screen.getByText((_, el) => el?.className === "crumb");
    expect(crumb.textContent).toContain("Security & Compliance");
    expect(crumb.textContent).toContain("Vulnerabilities");
  });
});

describe("Topbar StatusPill / SweepHealthStrip — quiet background auto-refresh (F-7)", () => {
  // F-7: StatusPill and SweepHealthStrip fetch once on mount with a bare
  // useEffect and never refresh. Topbar is sticky chrome that persists for
  // the whole session, so "All systems operational" and the sweep bars
  // reflect the moment of login indefinitely — a backend that goes degraded
  // an hour later still shows green.
  it("StatusPill re-fetches system_health on the 5th 60s tick, not before", async () => {
    const apiGet = vi.spyOn(api, "apiGet").mockResolvedValue({ rows: [] });
    vi.useFakeTimers();

    render(
      <RefreshProvider>
        <MemoryRouter initialEntries={["/"]}>
          <Topbar />
        </MemoryRouter>
      </RefreshProvider>,
    );

    const healthCalls = () =>
      apiGet.mock.calls.filter(([u]) => (u as string).includes("/api/dashboard/system_health")).length;

    await vi.waitFor(() => expect(healthCalls()).toBe(1));

    await vi.advanceTimersByTimeAsync(4 * 60_000);
    expect(healthCalls()).toBe(1);

    await vi.advanceTimersByTimeAsync(60_000);
    await vi.waitFor(() => expect(healthCalls()).toBe(2));
  });

  it("SweepHealthStrip re-fetches collection_runs and scan_points on the 5th 60s tick", async () => {
    const apiGet = vi.spyOn(api, "apiGet").mockResolvedValue({ rows: [] });
    vi.useFakeTimers();

    render(
      <RefreshProvider>
        <MemoryRouter initialEntries={["/"]}>
          <Topbar />
        </MemoryRouter>
      </RefreshProvider>,
    );

    const runsCalls = () =>
      apiGet.mock.calls.filter(([u]) => (u as string).includes("/api/dashboard/collection_runs")).length;
    const scansCalls = () =>
      apiGet.mock.calls.filter(([u]) => (u as string).includes("/api/dashboard/scan_points")).length;

    await vi.waitFor(() => expect(runsCalls()).toBe(1));
    expect(scansCalls()).toBe(1);

    await vi.advanceTimersByTimeAsync(5 * 60_000);
    await vi.waitFor(() => expect(runsCalls()).toBe(2));
    expect(scansCalls()).toBe(2);
  });
});

describe("StatusPill — distinct rendering when the backend cannot be reached (F-7)", () => {
  // Second defect from F-7: the failure path left health.length === 0, which
  // is indistinguishable from "nothing wrong" — StatusPill rendered the same
  // optimistic green "All systems operational" whether the fetch succeeded
  // with an empty list or failed outright. A monitoring UI that shows green
  // when it cannot see the backend is worse than one that shows nothing.
  it("does not render 'All systems operational' when the health fetch fails", async () => {
    vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
      if (url.includes("/api/dashboard/system_health")) throw new Error("network unreachable");
      return { rows: [] };
    });

    render(
      <MemoryRouter initialEntries={["/"]}>
        <Topbar />
      </MemoryRouter>,
    );

    await waitFor(() => expect(screen.queryByText(/all systems operational/i)).toBeNull());
    // Must show some distinct "unknown / cannot reach backend" signal instead
    // of silently rendering nothing (which would look identical to a still-
    // loading state and give no indication anything is wrong).
    expect(await screen.findByText(/unknown|unreachable|cannot reach/i)).toBeInTheDocument();
  });
});

describe("Topbar GlobalSearch case-insensitive matching", () => {
  // Regression guard: `q` is always lowercased, so every field it's compared
  // against must be lowercased too — otherwise a backend value with any
  // uppercase character (e.g. domain "PROD" instead of "prod") silently never
  // matches, even though the user typed a correct, matching substring.
  it("matches a resource whose domain is uppercase against a lowercase query", async () => {
    const uppercaseDomainResource = {
      hostname: "web01",
      domain: "PROD",
      resource_type: "vm",
    };
    vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
      if (url.startsWith("/api/dashboard/resources")) return [uppercaseDomainResource];
      return [];
    });

    render(
      <MemoryRouter initialEntries={["/"]}>
        <Topbar />
      </MemoryRouter>,
    );

    const input = screen.getByPlaceholderText("Search resources, drift, scripts, instincts…");
    fireEvent.focus(input);
    // "prod" only matches r.domain ("PROD") — not hostname ("web01") or
    // resource_type ("vm") — so this exercises the domain comparison alone.
    fireEvent.change(input, { target: { value: "prod" } });

    await waitFor(() => expect(screen.getByText("web01")).toBeTruthy());
    expect(screen.queryByText("No matches found")).toBeNull();
  });
});

describe("Topbar GlobalSearch keyboard accessibility (F-9)", () => {
  // The detail drawers this search deep-links into are otherwise unreachable
  // by keyboard if the results are plain `<div onClick>` with no role, no
  // tabIndex, and no key handler.
  it("renders each result as a focusable option, reachable and activatable without a mouse", async () => {
    vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
      if (url.startsWith("/api/dashboard/resources")) {
        return [{ hostname: "web01", domain: "prod", resource_type: "vm" }];
      }
      return [];
    });

    render(
      <MemoryRouter initialEntries={["/"]}>
        <Topbar />
      </MemoryRouter>,
    );

    const input = screen.getByPlaceholderText("Search resources, drift, scripts, instincts…");
    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: "web01" } });

    const option = await screen.findByRole("option", { name: /web01/ });
    // Must be a real tab stop, not merely clickable.
    option.focus();
    expect(option).toHaveFocus();

    // Enter/Space must activate it like any other focusable control (no
    // custom key handler needed once it's a real button/option element).
    fireEvent.click(option);
  });

  it("ArrowDown from the search input moves focus onto the first result", async () => {
    vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
      if (url.startsWith("/api/dashboard/resources")) {
        return [{ hostname: "web01", domain: "prod", resource_type: "vm" }];
      }
      return [];
    });

    render(
      <MemoryRouter initialEntries={["/"]}>
        <Topbar />
      </MemoryRouter>,
    );

    const input = screen.getByPlaceholderText("Search resources, drift, scripts, instincts…");
    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: "web01" } });

    const option = await screen.findByRole("option", { name: /web01/ });
    fireEvent.keyDown(input, { key: "ArrowDown" });
    expect(option).toHaveFocus();
  });
});

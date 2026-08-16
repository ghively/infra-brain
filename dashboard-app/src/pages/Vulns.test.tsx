import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import * as api from "../api";
import { Vulns } from "./Vulns";

/** Renders a stand-in for the destination page at the navigation target so
 *  the Host/CVE-cell drill-down tests can assert where onClick+navigate
 *  actually lands (F-1), not just that a click handler fired. */
function LocationProbe() {
  const location = useLocation();
  return <div data-testid="landed">{location.pathname + location.search}</div>;
}

function renderVulnsWithRoutes() {
  return render(
    <MemoryRouter initialEntries={["/vulns"]}>
      <Routes>
        <Route path="/vulns" element={<Vulns />} />
        <Route path="/hosts" element={<LocationProbe />} />
        <Route path="/r7detail" element={<LocationProbe />} />
      </Routes>
    </MemoryRouter>,
  );
}

afterEach(() => {
  // vitest.config.ts sets `globals: false`, so @testing-library/react's
  // automatic afterEach(cleanup) registration never fires — without this,
  // renders from earlier tests in this file accumulate in the jsdom document
  // (see Drift.test.tsx for the same pattern).
  cleanup();
  vi.restoreAllMocks();
});

// A page of vuln_queue rows mixing canonical-open ("open", "triage"),
// auto-closed ("resolved"), and human-closed ("remediated", "accepted_risk")
// statuses. Under the old buggy predicate (`status !== "resolved"`),
// "remediated" and "accepted_risk" would incorrectly count as open.
const MIXED_STATUS_ITEMS = [
  { cve: "CVE-2026-0001", host: "web01", pkg: "openssl", severity: "critical", cvss: 9.8, sla: "2d left", status: "open", priority: 150, pci_fail: false, exploits: 0 },
  { cve: "CVE-2026-0002", host: "web02", pkg: "curl", severity: "severe", cvss: 7.5, sla: "5d left", status: "triage", priority: 90, pci_fail: false, exploits: 0 },
  { cve: "CVE-2026-0003", host: "web03", pkg: "libxml2", severity: "moderate", cvss: 5.0, sla: "10d left", status: "resolved", priority: 20, pci_fail: false, exploits: 0 },
  { cve: "CVE-2026-0004", host: "web04", pkg: "zlib", severity: "critical", cvss: 9.0, sla: "1d left", status: "remediated", priority: 140, pci_fail: false, exploits: 0 },
  { cve: "CVE-2026-0005", host: "web05", pkg: "sudo", severity: "severe", cvss: 8.0, sla: "3d left", status: "accepted_risk", priority: 100, pci_fail: false, exploits: 0 },
];

function mockLoadSuccess(openCves = 42, criticalCves = 7, severeCves = 13) {
  vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
    if (url.includes("/api/dashboard/counts")) {
      return { open_cves: openCves, critical_cves: criticalCves, severe_cves: severeCves };
    }
    if (url.includes("/api/dashboard/vulnerabilities")) {
      return { items: MIXED_STATUS_ITEMS, total: 250, limit: 50, offset: 0 };
    }
    throw new Error(`unexpected apiGet: ${url}`);
  });
}

function renderVulns() {
  return render(
    <MemoryRouter>
      <Vulns />
    </MemoryRouter>,
  );
}

describe("Vulns page stat tiles/pills (TRK: fleet-wide accuracy audit)", () => {
  it("Open tile reflects the fleet-wide /api/dashboard/counts.open_cves value, not a page-derived count", async () => {
    mockLoadSuccess(42);

    renderVulns();
    await waitFor(() => expect(screen.getByText("CVE-2026-0001")).toBeInTheDocument());

    // The page (5 rows) has 4 rows that are NOT status "resolved" (open,
    // triage, remediated, accepted_risk) — if the old page-scoped
    // `status !== "resolved"` predicate were still driving the tile, it
    // would show 4, not the mocked fleet-wide 42.
    const openTileLabel = await screen.findByText("Open");
    const openTile = openTileLabel.parentElement;
    await waitFor(() => expect(openTile?.textContent).toContain("42"));
  });

  it("does NOT count remediated or accepted_risk rows as open (canonical open-status regression)", async () => {
    // A page containing only human-closed rows plus a couple of
    // canonical-open rows; open_cves is mocked as the *correct* backend
    // value (2, matching only the "open"/"triage" rows) to prove the tile
    // is sourced from the canonical-status-aware backend query rather than
    // any client-side `!== "resolved"` recomputation.
    mockLoadSuccess(2);

    renderVulns();
    await waitFor(() => expect(screen.getByText("CVE-2026-0004")).toBeInTheDocument());

    const openTileLabel = screen.getByText("Open");
    const openTile = openTileLabel.parentElement;
    expect(openTile?.textContent).toContain("2");
    // Guard against a regression back to page-scoped counting: the page has
    // 4 non-"resolved" rows (open, triage, remediated, accepted_risk) — if
    // that old predicate were reintroduced the tile would show 4, not 2.
    expect(openTile?.textContent).not.toContain("4");
  });

  it("Critical and Severe tiles reflect the fleet-wide /api/dashboard/counts aggregate, not a page-derived count (TRK-166 follow-up)", async () => {
    mockLoadSuccess(42, 7, 13);

    renderVulns();
    await waitFor(() => expect(screen.getByText("CVE-2026-0001")).toBeInTheDocument());

    // "Critical"/"Severe" also appear as severity-tab filter labels, so
    // disambiguate the stat tile by its accompanying numeric value.
    const criticalTileLabel = screen
      .getAllByText("Critical")
      .find((el) => el.parentElement?.textContent?.includes("7"));
    expect(criticalTileLabel).toBeDefined();
    const severeTileLabel = screen
      .getAllByText("Severe")
      .find((el) => el.parentElement?.textContent?.includes("13"));
    expect(severeTileLabel).toBeDefined();
  });

  it("Total tile still reflects the server-side envelope total, unaffected by these changes", async () => {
    mockLoadSuccess();

    renderVulns();
    await waitFor(() => expect(screen.getByText("CVE-2026-0001")).toBeInTheDocument());

    const totalTileLabel = screen.getByText("Total");
    const totalTile = totalTileLabel.parentElement;
    expect(totalTile?.textContent).toContain("250");
  });

  it("falls back to 0 on the Open tile if the counts fetch fails, without blanking the rest of the page", async () => {
    vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
      if (url.includes("/api/dashboard/counts")) throw new Error("network unreachable");
      if (url.includes("/api/dashboard/vulnerabilities")) {
        return { items: MIXED_STATUS_ITEMS, total: 250, limit: 50, offset: 0 };
      }
      throw new Error(`unexpected apiGet: ${url}`);
    });

    renderVulns();
    await waitFor(() => expect(screen.getByText("CVE-2026-0001")).toBeInTheDocument());

    const openTileLabel = screen.getByText("Open");
    const openTile = openTileLabel.parentElement;
    expect(openTile?.textContent).toContain("0");
  });
});

describe("Vulns page — Phase 1 DataTable/Panel rebuild (design-system proving ground)", () => {
  it("renders the CVE queue inside a DataTable with type-glyph headers and a read-only status-bar footer", async () => {
    mockLoadSuccess();
    renderVulns();
    await waitFor(() => expect(screen.getByText("CVE-2026-0001")).toBeInTheDocument());

    expect(screen.getByText(/read-only/i)).toBeInTheDocument();
    // Numeric-column glyph and FK-column glyph from the type-glyph legend.
    expect(screen.getAllByText("#").length).toBeGreaterThan(0);
    expect(screen.getAllByText("⇥").length).toBeGreaterThan(0);
  });

  it("renders a Rapid7 'severe' severity as the High pill, not as an unrecognized/NULL value (severity alias regression guard)", async () => {
    mockLoadSuccess();
    renderVulns();
    await waitFor(() => expect(screen.getByText("CVE-2026-0002")).toBeInTheDocument());

    // CVE-2026-0002 has severity "severe" in MIXED_STATUS_ITEMS.
    const row = screen.getByText("CVE-2026-0002").closest("tr");
    expect(row).not.toBeNull();
    expect(row?.textContent).toContain("High");
  });

  it("renders pci_fail=false as an em-dash, not the NULL token (false is a value, not missing data)", async () => {
    mockLoadSuccess();
    renderVulns();
    await waitFor(() => expect(screen.getByText("CVE-2026-0001")).toBeInTheDocument());

    // Every row in MIXED_STATUS_ITEMS has pci_fail: false.
    const row = screen.getByText("CVE-2026-0001").closest("tr");
    expect(row?.textContent).toContain("—");
    expect(row?.textContent).not.toContain("NULL");
  });

  it("renders host as a click-through FkCell button, not a raw <a href> (F-1: a raw href escapes the router basename)", async () => {
    mockLoadSuccess();
    renderVulns();
    await waitFor(() => expect(screen.getByText("CVE-2026-0001")).toBeInTheDocument());

    // A raw `<a href>` escapes the router's `basename="/dashboard2"`
    // (App.tsx) into a hard-navigation 404 against the backend, which serves
    // the SPA only at /dashboard2. FkCell's `onClick` prop renders a real
    // `<button>` instead (cells.tsx) — there must be no `<a>` here at all.
    expect(screen.getByText("web01").closest("a")).toBeNull();
    expect(screen.getByText("web01").closest("button")).not.toBeNull();
  });

  it("links the host to a Hosts-page query shape the Hosts page actually consumes (?tab=hosts&q=..., not the dead ?host=)", async () => {
    // Regression guard: Hosts.tsx only reads `?tab=`, and its hosts-list
    // child UnifiedHostsTab.tsx only reads `?q=` — neither reads `?host=`.
    // A link built with `?host=` silently lands on Hosts unfiltered. This
    // asserts the shape Hosts/UnifiedHostsTab actually consume (the same
    // shape Invrec.tsx already uses for the identical host->Hosts
    // drill-down), so a regression back to `?host=` fails this test.
    mockLoadSuccess();
    renderVulnsWithRoutes();
    await waitFor(() => expect(screen.getByText("CVE-2026-0001")).toBeInTheDocument());

    fireEvent.click(screen.getByText("web01"));
    await waitFor(() => expect(screen.getByTestId("landed")).toHaveTextContent("/hosts?tab=hosts&q=web01"));
  });

  it("links each CVE through to the CVE Detail drill-in view, via basename-safe onClick+navigate (not a raw href)", async () => {
    mockLoadSuccess();
    renderVulnsWithRoutes();
    await waitFor(() => expect(screen.getByText("CVE-2026-0001")).toBeInTheDocument());

    expect(screen.getByText("CVE-2026-0001").closest("a")).toBeNull();

    fireEvent.click(screen.getByText("CVE-2026-0001"));
    await waitFor(() => expect(screen.getByTestId("landed")).toHaveTextContent("/r7detail?cve=CVE-2026-0001"));
  });

  it("distinguishes 'Rapid7 not configured' (not-configured) from 'filters matched nothing' (filter-zero)", async () => {
    vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
      if (url.includes("/api/dashboard/counts")) return { open_cves: 0 };
      if (url.includes("/api/dashboard/vulnerabilities")) return { items: [], total: 0, limit: 50, offset: 0 };
      throw new Error(`unexpected apiGet: ${url}`);
    });

    renderVulns();
    await waitFor(() =>
      expect(screen.getByText("Vulnerability scanning is not configured")).toBeInTheDocument(),
    );
    expect(screen.getByText("Vulnerability scanning is not configured").closest("[data-empty-kind]")).toHaveAttribute(
      "data-empty-kind",
      "not-configured",
    );
    expect(screen.queryByText(/match the current filters/i)).not.toBeInTheDocument();
  });

  it("shows the filter-zero EmptyState (with a clear-filters action) when a filter excludes all rows", async () => {
    vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
      if (url.includes("/api/dashboard/counts")) return { open_cves: 5 };
      if (url.includes("/api/dashboard/vulnerabilities")) {
        // Severity filter present in the query string but the mocked backend
        // still returns zero rows — simulates "this host has no critical CVEs".
        return { items: [], total: 0, limit: 50, offset: 0 };
      }
      throw new Error(`unexpected apiGet: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/vulns?host=web99"]}>
        <Vulns />
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByText(/match the current filters/i)).toBeInTheDocument());
    expect(screen.getByRole("button", { name: /clear filters/i })).toBeInTheDocument();
  });

  it("shows an error EmptyState (not a bare alert div) when the vulnerabilities fetch fails", async () => {
    vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
      if (url.includes("/api/dashboard/counts")) return { open_cves: 0 };
      if (url.includes("/api/dashboard/vulnerabilities")) throw new Error("db unreachable");
      throw new Error(`unexpected apiGet: ${url}`);
    });

    renderVulns();
    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
    expect(screen.getByRole("alert").textContent).toMatch(/failed to load/i);
  });
});

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import * as api from "../api";
import { NetDiscovery } from "./NetDiscovery";

afterEach(() => {
  // vitest.config.ts sets `globals: false`, so @testing-library/react's
  // automatic afterEach(cleanup) registration never fires — without this,
  // renders from earlier tests in this file accumulate in the jsdom document
  // (see Vulns.test.tsx/Drift.test.tsx for the same pattern).
  cleanup();
  vi.restoreAllMocks();
});

const HOSTS = [
  {
    id: "h1",
    ip: "10.0.0.5",
    mac: "AA:BB:CC:DD:EE:01",
    mac_vendor: "Acme NIC Co",
    hostname: "web01",
    first_seen: "2026-07-01T00:00:00Z",
    last_seen: "2026-07-27T12:00:00Z",
    responded: true,
    discovery_tier: "active",
    is_fragile: false,
    is_known: true,
    is_shadow_it: false,
    threat_level: "none",
    zone: "dmz",
    resource_id: "r1",
    services: [],
  },
  {
    id: "h2",
    ip: "10.0.0.99",
    mac: "AA:BB:CC:DD:EE:02",
    mac_vendor: "",
    hostname: "",
    first_seen: "2026-07-20T00:00:00Z",
    last_seen: "2026-07-27T10:00:00Z",
    responded: true,
    discovery_tier: "passive",
    is_fragile: false,
    is_known: false,
    is_shadow_it: true,
    threat_level: "garbled-value",
    zone: "",
    resource_id: null,
    services: [
      {
        port: 4444,
        proto: "tcp",
        service: "unknown",
        banner: "",
        fingerprint: "",
        is_dangerous: true,
        is_suspicious: false,
        last_seen: "2026-07-27T10:00:00Z",
      },
    ],
  },
];

function mockLoadSuccess() {
  vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
    if (url.includes("/api/dashboard/netdiscovery/hosts")) {
      return {
        items: HOSTS,
        total: 2,
        limit: 100,
        offset: 0,
        summary: { total_hosts: 2, shadow_it_count: 1, known_count: 1, by_threat_level: { none: 1 } },
      };
    }
    throw new Error(`unexpected apiGet: ${url}`);
  });
}

function renderPage() {
  return render(
    <MemoryRouter>
      <NetDiscovery />
    </MemoryRouter>,
  );
}

describe("NetDiscovery page — Phase 3 DataTable/Panel rebuild", () => {
  it("renders discovered hosts in a DataTable with a read-only status-bar footer", async () => {
    mockLoadSuccess();
    renderPage();
    await waitFor(() => expect(screen.getByText("10.0.0.5")).toBeInTheDocument());

    expect(screen.getByText("read-only · no mutations possible from this view")).toBeInTheDocument();
    expect(screen.getByText("web01")).toBeInTheDocument();
  });

  it("renders an unrecognized threat_level distinctly from confirmed-safe 'none' (TRK-176 Wave 2 regression guard)", async () => {
    mockLoadSuccess();
    renderPage();
    await waitFor(() => expect(screen.getByText("10.0.0.99")).toBeInTheDocument());

    const noneRow = screen.getByText("10.0.0.5").closest("tr");
    const unknownRow = screen.getByText("10.0.0.99").closest("tr");
    const noneBadge = noneRow?.querySelector(".ib-sev");
    const unknownBadge = unknownRow?.querySelector(".ib-sev");

    expect(noneBadge).not.toBeNull();
    expect(unknownBadge).not.toBeNull();
    // The two must NOT share a tone class — "none" is ok/green, the garbled
    // value must fall back to neutral/gray, never silently reuse "ok".
    expect(unknownBadge?.className).not.toEqual(noneBadge?.className);
    expect(unknownBadge?.className).toContain("ib-sev-low"); // neutral tone class
  });

  it("filters by threat-level tab and resets pagination", async () => {
    mockLoadSuccess();
    renderPage();
    await waitFor(() => expect(screen.getByText("10.0.0.5")).toBeInTheDocument());

    vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
      expect(url).toContain("threat_level=high");
      return {
        items: [],
        total: 0,
        limit: 100,
        offset: 0,
        summary: { total_hosts: 2, shadow_it_count: 1, known_count: 1, by_threat_level: {} },
      };
    });

    screen.getByRole("button", { name: "High" }).click();
    await waitFor(() => expect(screen.getByText(/match the current filters/i)).toBeInTheDocument());
  });

  it("toggles the Shadow IT only filter", async () => {
    mockLoadSuccess();
    renderPage();
    await waitFor(() => expect(screen.getByText("10.0.0.5")).toBeInTheDocument());

    vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
      expect(url).toContain("is_shadow_it=true");
      return {
        items: [HOSTS[1]],
        total: 1,
        limit: 100,
        offset: 0,
        summary: { total_hosts: 2, shadow_it_count: 1, known_count: 1, by_threat_level: {} },
      };
    });

    screen.getByRole("button", { name: "Shadow IT only" }).click();
    await waitFor(() => expect(screen.getByText("10.0.0.99")).toBeInTheDocument());
  });

  it("opens the detail drawer with the host's discovered services on row click", async () => {
    mockLoadSuccess();
    renderPage();
    await waitFor(() => expect(screen.getByText("10.0.0.99")).toBeInTheDocument());

    (screen.getByText("10.0.0.99").closest("tr") as HTMLElement).click();

    await waitFor(() => expect(screen.getByText("10.0.0.99 — discovered host")).toBeInTheDocument());
    expect(screen.getByText("4444/tcp")).toBeInTheDocument();
    expect(screen.getByText("dangerous")).toBeInTheDocument();
  });

  it("distinguishes 'no hosts discovered yet' (none-yet) from 'filters matched nothing' (filter-zero)", async () => {
    vi.spyOn(api, "apiGet").mockImplementation(async () => ({
      items: [],
      total: 0,
      limit: 100,
      offset: 0,
      summary: { total_hosts: 0, shadow_it_count: 0, known_count: 0, by_threat_level: {} },
    }));

    renderPage();
    await waitFor(() => expect(screen.getByText("No hosts discovered yet")).toBeInTheDocument());
    expect(screen.queryByText(/match the current filters/i)).not.toBeInTheDocument();
  });

  it("shows an error EmptyState when the hosts fetch fails", async () => {
    vi.spyOn(api, "apiGet").mockImplementation(async () => {
      throw new Error("db unreachable");
    });

    renderPage();
    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
    expect(screen.getByRole("alert").textContent).toMatch(/failed to load/i);
  });
});

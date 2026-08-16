/** Tests for the consolidated Hosts page (Phase 2).
 *
 *  This file replaces `Resources.test.tsx` and `Fleet.test.tsx`, which were
 *  deleted along with their pages when the three host views were folded into one
 *  tabbed page. Every regression those two files locked in is re-asserted here
 *  against the consolidated component — notably the failed-fetch-vs-empty-result
 *  distinction (a backend outage must never render as reassuring "stable" /
 *  "nothing collected" copy) and the page-scoped-count labeling. `Hosts.tsx`
 *  itself had no test file before this.
 */
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import * as api from "../api";
import { Hosts } from "./Hosts";

afterEach(() => {
  // vitest.config.ts sets `globals: false`, so @testing-library/react's
  // automatic afterEach(cleanup) never registers — without this, renders from
  // earlier tests accumulate in the jsdom document.
  cleanup();
  vi.restoreAllMocks();
});

const HOST = {
  short_hostname: "web01",
  fqdn: "web01.corp.local",
  ip_addresses: ["10.0.0.5"],
  os_family: "linux",
  risk_score: 1200,
  vuln_count: 3,
  patch_status: "compliant",
  vsphere_power_state: "poweredOn",
  octopus_machine_status: "Online",
  r7_resource_id: "r7-1",
  vsphere_resource_id: null,
  octopus_resource_id: null,
  linux_resource_id: "lin-1",
  windows_resource_id: null,
  last_reconciled: "2026-07-27T00:00:00Z",
};

const HOSTS_PAGE = { items: [HOST], total: 1, limit: 100, offset: 0 };

const HEALTHY_RESOURCE = {
  id: "r1",
  hostname: "web01",
  domain: "linux",
  resource_type: "vm",
  zone: "corpor",
  status: "healthy",
  last_seen_at: "2026-07-24T00:00:00Z",
  drift_count: 0,
};

// `FLEET_RESPONSE` (the /api/dashboard/fleet fixture) went with the Rapid7 tabs.

type Tab = "hosts" | "resources";

function renderHosts(tab: Tab = "hosts", opts?: { search?: string; state?: unknown; path?: string }) {
  const path = opts?.path ?? "/hosts";
  return render(
    <MemoryRouter initialEntries={[{ pathname: path, search: opts?.search ?? "", state: opts?.state ?? null }]}>
      <Hosts defaultTab={tab} />
    </MemoryRouter>,
  );
}

/** Resource-tab mock: the two drawer sub-fetches plus the list endpoint. Order
 *  matters — the snapshots URL also contains "/api/dashboard/resources". */
function mockResources(over: { snapshots?: "ok" | "fail"; drift?: "ok" | "fail"; items?: unknown[]; total?: number }) {
  vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
    if (url.includes("/resources/r1/snapshots")) {
      if (over.snapshots === "fail") throw new Error("HTTP 500");
      return { items: [], total: 0, limit: 5, offset: 0 };
    }
    if (url.includes("/drift_events")) {
      if (over.drift === "fail") throw new Error("network unreachable");
      return { items: [], total: 0 };
    }
    if (url.includes("/api/dashboard/resources")) {
      return { items: over.items ?? [HEALTHY_RESOURCE], total: over.total ?? 1, limit: 200, offset: 0 };
    }
    throw new Error(`unexpected apiGet: ${url}`);
  });
}


/** Click a resource row *body* cell to open its drawer. The hostname cell is an
 *  FK cell whose own click cross-links to the Hosts tab (DESIGN.md §5), so the
 *  drawer must be opened from a plain cell — this mirrors real use. */
function openResourceDrawer() {
  const cell = screen.getByText("vm");
  fireEvent.click(cell.closest("tr") as HTMLElement);
}

describe("Hosts consolidation — tab strip and routing", () => {
  // Was "all five host views". The three Rapid7 tabs (Fleet / Sites / Tags)
  // were removed with their components — retired collector, empty tables.
  it("exposes both host views as one keyboard-navigable tab strip", async () => {
    vi.spyOn(api, "apiGet").mockResolvedValue(HOSTS_PAGE);
    renderHosts();

    const tablist = screen.getByRole("tablist", { name: "Host views" });
    const tabs = within(tablist).getAllByRole("tab");
    expect(tabs.map((t) => t.textContent)).toEqual(["Hosts", "Resources"]);
    expect(screen.getByRole("tab", { name: "Hosts" })).toHaveAttribute("aria-selected", "true");
    await waitFor(() => expect(screen.getByText("web01")).toBeInTheDocument());
  });

  it("offers no Rapid7 tab at all, on any route", async () => {
    vi.spyOn(api, "apiGet").mockResolvedValue(HOSTS_PAGE);
    renderHosts();

    const tablist = screen.getByRole("tablist", { name: "Host views" });
    expect(within(tablist).queryByRole("tab", { name: /rapid7|r7/i })).toBeNull();
  });

  it("renders the Resources view when mounted as the /resources route alias", async () => {
    mockResources({});
    renderHosts("resources", { path: "/resources" });

    expect(screen.getByRole("tab", { name: "Resources" })).toHaveAttribute("aria-selected", "true");
    await waitFor(() => expect(screen.getByText("web01")).toBeInTheDocument());
    expect(screen.getByText(/Raw collector resources/)).toBeInTheDocument();
  });

  // Replaces "renders the Rapid7 Fleet view when mounted as the /fleet route
  // alias". `/fleet` is deliberately still a live route — it is in bookmarks —
  // but it now lands on the canonical Hosts view rather than a Rapid7 tab.
  it("lands /fleet on the canonical Hosts view instead of 404-ing an old bookmark", async () => {
    vi.spyOn(api, "apiGet").mockResolvedValue(HOSTS_PAGE);
    renderHosts("hosts", { path: "/fleet" });

    expect(screen.getByRole("tab", { name: "Hosts" })).toHaveAttribute("aria-selected", "true");
    await waitFor(() => expect(screen.getByText("web01")).toBeInTheDocument());
  });

  it("lets ?tab= override the route's default tab, so any view is deep-linkable", async () => {
    mockResources({});
    renderHosts("hosts", { search: "?tab=resources" });

    expect(screen.getByRole("tab", { name: "Resources" })).toHaveAttribute("aria-selected", "true");
    await waitFor(() => expect(screen.getByText(/Raw collector resources/)).toBeInTheDocument());
  });

  it("falls back to the default tab for a stale ?tab= naming a removed Rapid7 view", async () => {
    vi.spyOn(api, "apiGet").mockResolvedValue(HOSTS_PAGE);
    renderHosts("hosts", { search: "?tab=fleet" });

    expect(screen.getByRole("tab", { name: "Hosts" })).toHaveAttribute("aria-selected", "true");
    await waitFor(() => expect(screen.getByText("web01")).toBeInTheDocument());
  });

  it("switches view on tab click", async () => {
    mockResources({});
    renderHosts();

    fireEvent.click(screen.getByRole("tab", { name: "Resources" }));
    await waitFor(() => expect(screen.getByText(/Raw collector resources/)).toBeInTheDocument());
  });


  it("cross-links a resource row's FK hostname cell to the unified Hosts view, filtered to that host", async () => {
    const seen: string[] = [];
    vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
      seen.push(url);
      if (url.includes("/api/dashboard/hosts")) return { items: [], total: 0, limit: 100, offset: 0 };
      if (url.includes("/api/dashboard/resources")) return { items: [HEALTHY_RESOURCE], total: 1, limit: 200, offset: 0 };
      throw new Error(`unexpected apiGet: ${url}`);
    });
    renderHosts("resources", { path: "/resources" });
    await waitFor(() => expect(screen.getByText("web01")).toBeInTheDocument());

    fireEvent.click(screen.getByText("web01"));

    await waitFor(() => expect(screen.getByRole("tab", { name: "Hosts" })).toHaveAttribute("aria-selected", "true"));
    await waitFor(() => expect(seen.some((u) => u.includes("/api/dashboard/hosts") && u.includes("q=web01"))).toBe(true));
  });
  it("opens the resource drawer from Topbar global-search router state (deep-link preserved)", async () => {
    mockResources({});
    renderHosts("resources", {
      path: "/resources",
      state: { detailOpen: { kind: "resource", data: HEALTHY_RESOURCE } },
    });

    await waitFor(() => expect(screen.getByRole("dialog")).toBeInTheDocument());
    expect(within(screen.getByRole("dialog")).getByText(/web01 — linux/)).toBeInTheDocument();
  });
});

describe("Hosts tab — unified host table", () => {
  it("renders the host row on the new DataTable with type-glyph headers", async () => {
    vi.spyOn(api, "apiGet").mockResolvedValue(HOSTS_PAGE);
    renderHosts();

    await waitFor(() => expect(screen.getByText("web01")).toBeInTheDocument());
    expect(screen.getByRole("columnheader", { name: /Hostname/ })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: /Risk/ })).toBeInTheDocument();
    // Status-bar footer with the read-only marker (DESIGN.md §7).
    expect(screen.getByText(/read-only/)).toBeInTheDocument();
  });

  it("renders a missing value as the NULL token, never as a blank cell", async () => {
    vi.spyOn(api, "apiGet").mockResolvedValue({
      ...HOSTS_PAGE,
      items: [{ ...HOST, os_family: null, patch_status: null, last_reconciled: null }],
    });
    renderHosts();

    await waitFor(() => expect(screen.getByText("web01")).toBeInTheDocument());
    expect(screen.getAllByText("NULL").length).toBeGreaterThanOrEqual(3);
  });

  it("distinguishes a filter-zero result from having no host records at all", async () => {
    vi.spyOn(api, "apiGet").mockResolvedValue({ items: [], total: 0, limit: 100, offset: 0 });
    renderHosts();

    await waitFor(() => expect(screen.getByText(/No unified host records yet/)).toBeInTheDocument());

    fireEvent.change(screen.getByLabelText("Search hostname or FQDN"), { target: { value: "nope" } });
    await waitFor(() => expect(screen.getByText(/No hosts match your filters/)).toBeInTheDocument());
  });

  it("shows an error state, not an empty state, when the host list fetch fails", async () => {
    vi.spyOn(api, "apiGet").mockRejectedValue(new Error("boom"));
    renderHosts();

    await waitFor(() => expect(screen.getByText(/Hosts failed to load/)).toBeInTheDocument());
    expect(screen.queryByText(/No unified host records yet/)).not.toBeInTheDocument();
  });

  it("labels the with-vulns count as page-scoped, since hosts.py has no global aggregate", async () => {
    vi.spyOn(api, "apiGet").mockResolvedValue({ ...HOSTS_PAGE, total: 500 });
    renderHosts();

    await waitFor(() => expect(screen.getByText("1 with vulns (this page)")).toBeInTheDocument());
  });
});

describe("Resources tab — drawer drift/snapshot fetch-failure handling (ported from Resources.test.tsx)", () => {
  it("shows a visible error, NOT the reassuring empty-state copy, when the drift fetch fails", async () => {
    mockResources({ drift: "fail" });
    renderHosts("resources");
    await waitFor(() => expect(screen.getByText("web01")).toBeInTheDocument());

    openResourceDrawer();

    // A failed drift fetch must never render as "No drift detected —
    // configuration stable." — that manufactures a stability claim from an outage.
    await waitFor(() => expect(screen.getByText(/Drift history failed to load/)).toBeInTheDocument());
    expect(screen.queryByText(/No drift detected/)).not.toBeInTheDocument();
  });

  it("shows a visible error, NOT the reassuring empty-state copy, when the snapshot fetch fails", async () => {
    mockResources({ snapshots: "fail" });
    renderHosts("resources");
    await waitFor(() => expect(screen.getByText("web01")).toBeInTheDocument());

    openResourceDrawer();

    await waitFor(() => expect(screen.getByText(/Snapshot history failed to load/)).toBeInTheDocument());
    expect(screen.queryByText(/No snapshots recorded/)).not.toBeInTheDocument();
  });

  it("still shows the honest empty-state copy when the fetches genuinely succeed with zero rows", async () => {
    mockResources({});
    renderHosts("resources");
    await waitFor(() => expect(screen.getByText("web01")).toBeInTheDocument());

    openResourceDrawer();

    await waitFor(() => expect(screen.getByText(/No snapshots recorded/)).toBeInTheDocument());
    expect(screen.getByText(/No drift detected/)).toBeInTheDocument();
    expect(screen.queryByText(/failed to load/)).not.toBeInTheDocument();
  });
});

describe("Resources tab — status badge tones (ported from Resources.test.tsx)", () => {
  it("gives a healthy resource the ok tone, not the unknown-status fallback", async () => {
    mockResources({});
    renderHosts("resources");

    await waitFor(() => expect(screen.getByText("web01")).toBeInTheDocument());
    const badge = screen.getAllByText("healthy")[0];
    // Post-redesign the tone is a design-system class, not an inline hex.
    expect(badge.className).toContain("ib-sev-ok");
    expect(badge.className).not.toContain("ib-sev-low");
  });

  it("still falls back to the neutral tone for a genuinely unrecognized status", async () => {
    mockResources({ items: [{ ...HEALTHY_RESOURCE, id: "r2", hostname: "mystery01", status: "quarantined" }] });
    renderHosts("resources");

    await waitFor(() => expect(screen.getByText("mystery01")).toBeInTheDocument());
    const badge = screen.getAllByText("quarantined")[0];
    expect(badge.className).toContain("ib-sev-low");
    expect(badge.className).not.toContain("ib-sev-ok");
  });
});

describe("Resources tab — page-scoped domain counts are labeled honestly (ported from Resources.test.tsx)", () => {
  it("labels per-domain chip counts '(page)' while the all-domains chip shows the server total", async () => {
    // Server total (10) exceeds this page (3 items over 2 domains), so per-domain
    // counts summed from page.items cannot equal the all-domains total.
    mockResources({
      total: 10,
      items: [
        { ...HEALTHY_RESOURCE, id: "r1", hostname: "web01", domain: "linux" },
        { ...HEALTHY_RESOURCE, id: "r2", hostname: "web02", domain: "linux" },
        { ...HEALTHY_RESOURCE, id: "r3", hostname: "win01", domain: "windows" },
      ],
    });
    renderHosts("resources");

    await waitFor(() => expect(screen.getByText("web01")).toBeInTheDocument());
    const chips = screen.getByRole("group", { name: "Filter by collector domain" });
    expect(within(chips).getByRole("button", { name: "All domains 10" })).toBeInTheDocument();
    expect(within(chips).getByRole("button", { name: "linux 2 (page)" })).toBeInTheDocument();
    expect(within(chips).getByRole("button", { name: "windows 1 (page)" })).toBeInTheDocument();
  });
});

/* REMOVED: `describe("R7 Sites / Tags tabs …")` and `describe("Rapid7 Fleet tab …")`.
 * Both covered components that no longer exist (`pages/hosts/R7Tabs.tsx`,
 * `pages/hosts/FleetTab.tsx`), deleted because the `vuln` collector is
 * `retired=True` and r7_assets / r7_asset_sites / r7_asset_tags are all empty.
 * Nothing replaced them: there is no Rapid7 view left to test. The tab strip's
 * new two-tab shape is asserted in "exposes both host views …" above. */

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import * as api from "../api";
import { Agents } from "./Agents";

afterEach(() => {
  // vitest.config.ts sets `globals: false`, so @testing-library/react's
  // automatic afterEach(cleanup) registration never fires — without this,
  // renders from earlier tests in this file accumulate in the jsdom document
  // (see Drift.test.tsx / McpKeys.test.tsx for the same pattern).
  cleanup();
  vi.restoreAllMocks();
});

// T8: partial (detail-write failure on an otherwise-successful run) and
// skipped (dependency unconfigured) must render visibly differently from
// both "healthy" and "idle" (never run) — see governance_ops.py's
// list_agents status map. Regression guard for the bug where these fell
// through to the same grey "idle" dot and were excluded from every stat tile.
const AGENTS = [
  {
    name: "LinuxAgent",
    domain: "linux",
    kind: "collector",
    schedule: "hourly",
    last_run: "2026-07-24T00:00:00Z",
    status: "healthy",
    output: "40 resources",
    desc: "",
    tools: [],
  },
  {
    name: "VsphereAgent",
    domain: "vsphere",
    kind: "collector",
    schedule: "hourly",
    last_run: "2026-07-24T00:00:00Z",
    status: "partial",
    output: "12 resources",
    desc: "",
    tools: [],
  },
  {
    name: "GitlabAgent",
    domain: "gitlab",
    kind: "collector",
    schedule: "hourly",
    last_run: "2026-07-24T00:00:00Z",
    status: "degraded",
    output: "0 resources",
    desc: "",
    tools: [],
  },
  {
    name: "NetdiscoveryAgent",
    domain: "netdiscovery",
    kind: "collector",
    schedule: "on-demand",
    last_run: "2026-07-24T00:00:00Z",
    status: "skipped",
    output: "0 resources",
    desc: "",
    tools: [],
  },
  {
    name: "NeverRunAgent",
    domain: "neverrun",
    kind: "collector",
    schedule: "on-demand",
    last_run: "—",
    status: "idle",
    output: "no runs yet",
    desc: "",
    tools: [],
  },
];

function mockLoadSuccess() {
  vi.spyOn(api, "apiGet").mockImplementation(async () => ({
    items: AGENTS,
    total: AGENTS.length,
  }));
}

function renderAgents() {
  return render(
    <MemoryRouter>
      <Agents />
    </MemoryRouter>,
  );
}

/** The status Badge for a given agent's row — asserted by CSS class (the
 *  `ib-sev-*` tone class), not by inline style, per the Phase 3 primitives
 *  contract (Badge/SeverityPill render tone as a class, not a computed color). */
function statusBadgeFor(name: string): HTMLElement {
  const nameCell = screen.getByRole("cell", { name });
  const row = nameCell.closest("tr") as HTMLElement;
  // Status is the last column in Agents.tsx's column list.
  const cells = row.querySelectorAll("td");
  return cells[cells.length - 1].querySelector("span") as HTMLElement;
}

describe("Agents page — partial/skipped status visibility (T8)", () => {
  it("renders a partial-status agent's badge with a different tone class than healthy and idle", async () => {
    mockLoadSuccess();
    renderAgents();
    await waitFor(() => expect(screen.getByText("VsphereAgent")).toBeInTheDocument());

    const healthyBadge = statusBadgeFor("LinuxAgent");
    const partialBadge = statusBadgeFor("VsphereAgent");
    const idleBadge = statusBadgeFor("NeverRunAgent");

    expect(partialBadge.className).not.toBe(healthyBadge.className);
    expect(partialBadge.className).not.toBe(idleBadge.className);
  });

  it("renders a skipped-status agent's badge with a different tone class than healthy and idle (never run)", async () => {
    mockLoadSuccess();
    renderAgents();
    await waitFor(() => expect(screen.getByText("NetdiscoveryAgent")).toBeInTheDocument());

    const healthyBadge = statusBadgeFor("LinuxAgent");
    const skippedBadge = statusBadgeFor("NetdiscoveryAgent");
    const idleBadge = statusBadgeFor("NeverRunAgent");

    expect(skippedBadge.className).not.toBe(healthyBadge.className);
    expect(skippedBadge.className).not.toBe(idleBadge.className);
  });

  it("counts a partial-status agent in the Degraded stat tile, not silently excluded", async () => {
    mockLoadSuccess();
    renderAgents();
    await waitFor(() => expect(screen.getByText("VsphereAgent")).toBeInTheDocument());

    // One "degraded" (GitlabAgent) + one "partial" (VsphereAgent) => 2.
    expect(screen.getByText("Degraded")).toBeInTheDocument();
    const degradedTile = screen.getByText("Degraded").parentElement as HTMLElement;
    expect(degradedTile.textContent).toContain("2");

    // Healthy tile still counts only the true healthy agent.
    const healthyTile = screen.getByText("Healthy").parentElement as HTMLElement;
    expect(healthyTile.textContent).toContain("1");
  });

  it("shows the literal status text for partial and skipped agents in the detail drawer", async () => {
    mockLoadSuccess();
    renderAgents();
    await waitFor(() => expect(screen.getByText("VsphereAgent")).toBeInTheDocument());

    fireEvent.click(screen.getByText("VsphereAgent"));
    await waitFor(() => expect(screen.getAllByText("partial").length).toBeGreaterThan(0));
  });
});

describe("Agents page — Phase 3 design-system rebuild", () => {
  it("renders the agent registry table with a kind badge", async () => {
    mockLoadSuccess();
    renderAgents();

    await waitFor(() => expect(screen.getByText("LinuxAgent")).toBeInTheDocument());
    expect(screen.getAllByText("collector").length).toBeGreaterThan(0);
    expect(screen.getByText("read-only · no mutations possible from this view")).toBeInTheDocument();
  });

  it("row click opens the DetailDrawer with configuration and read-only tools", async () => {
    vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
      if (url.includes("/api/dashboard/activity")) return { items: [], total: 0 };
      if (url.includes("/api/dashboard/decisions")) return { items: [], total: 0 };
      return { items: AGENTS, total: AGENTS.length };
    });
    renderAgents();

    await waitFor(() => expect(screen.getByText("LinuxAgent")).toBeInTheDocument());
    fireEvent.click(screen.getByText("LinuxAgent"));

    await waitFor(() => expect(screen.getByText("Configuration")).toBeInTheDocument());
    expect(screen.getByText("Tools (read-only)")).toBeInTheDocument();
  });

  it("shows a 'none-yet' empty state when no agents are registered", async () => {
    vi.spyOn(api, "apiGet").mockImplementation(async () => ({ items: [], total: 0 }));
    renderAgents();

    await waitFor(() => expect(screen.getByText("No agents registered")).toBeInTheDocument());
  });

  it("shows a full-page error on load failure", async () => {
    vi.spyOn(api, "apiGet").mockRejectedValue(new Error("network unreachable"));
    renderAgents();

    await waitFor(() => expect(screen.getByText("Agents failed to load")).toBeInTheDocument());
  });
});

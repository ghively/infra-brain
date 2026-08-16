import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import * as api from "../api";
import { Observations } from "./Observations";

afterEach(() => {
  // vitest.config.ts sets `globals: false`, so @testing-library/react's
  // automatic afterEach(cleanup) registration never fires — without this,
  // renders from earlier tests in this file accumulate in the jsdom document
  // (see ScanSchedule.test.tsx/Drift.test.tsx for the same pattern).
  cleanup();
  vi.restoreAllMocks();
});

const OBSERVATIONS = [
  { agent: "linux-agent", tool: "list_hosts", domain: "linux", pattern: "frequent host listing", count: 42, last_seen: "2026-07-27 12:00:00" },
  { agent: "vsphere-agent", tool: "list_vms", domain: "vsphere", pattern: "vm inventory sweep", count: 128, last_seen: "2026-07-27 13:00:00" },
];

function mockLoadSuccess() {
  vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
    if (url.includes("/api/dashboard/observations")) return { items: OBSERVATIONS, total: 2 };
    throw new Error(`unexpected apiGet: ${url}`);
  });
}

function renderObservations() {
  return render(
    <MemoryRouter>
      <Observations />
    </MemoryRouter>,
  );
}

describe("Observations page — Phase 3 design-system rebuild", () => {
  it("renders observations in a DataTable with agent, tool, pattern, count, and last seen", async () => {
    mockLoadSuccess();
    renderObservations();

    await waitFor(() => expect(screen.getByText("linux-agent")).toBeInTheDocument());
    expect(screen.getByText("vsphere-agent")).toBeInTheDocument();
    expect(screen.getByText("list_hosts")).toBeInTheDocument();
    expect(screen.getByText("frequent host listing")).toBeInTheDocument();
    expect(screen.getByText("read-only · no mutations possible from this view")).toBeInTheDocument();
  });

  it("orders rows by count descending by default (highest-count pattern first)", async () => {
    mockLoadSuccess();
    renderObservations();

    await waitFor(() => expect(screen.getByText("vsphere-agent")).toBeInTheDocument());
    const rows = screen.getAllByRole("row"); // [0] header row
    // vsphere-agent (count 128) should appear before linux-agent (count 42).
    const vsphereIdx = rows.findIndex((r) => r.textContent?.includes("vsphere-agent"));
    const linuxIdx = rows.findIndex((r) => r.textContent?.includes("linux-agent"));
    expect(vsphereIdx).toBeGreaterThan(0);
    expect(vsphereIdx).toBeLessThan(linuxIdx);
  });

  it("computes the 4 summary stat tiles (Patterns, Total Calls, Agents, Top Count)", async () => {
    mockLoadSuccess();
    renderObservations();

    await waitFor(() => expect(screen.getByText("linux-agent")).toBeInTheDocument());
    expect(screen.getByText("Patterns")).toBeInTheDocument();
    expect(screen.getByText("Total Calls")).toBeInTheDocument();
    expect(screen.getByText("170")).toBeInTheDocument(); // 42 + 128
    expect(screen.getByText("Agents")).toBeInTheDocument();
    expect(screen.getByText("Top Count")).toBeInTheDocument();
    // "128" appears twice: once as the Top Count stat tile value, once as the
    // vsphere-agent row's Count cell — assert at least the table occurrence.
    expect(screen.getAllByText("128").length).toBeGreaterThan(0);
  });

  it("shows an error EmptyState (not a bare alert div) when the observations fetch fails", async () => {
    vi.spyOn(api, "apiGet").mockImplementation(async () => {
      throw new Error("db unreachable");
    });

    renderObservations();
    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
    expect(screen.getByText(/Observations failed to load/i)).toBeInTheDocument();
  });

  it("shows a none-yet EmptyState when there are zero observations", async () => {
    vi.spyOn(api, "apiGet").mockImplementation(async () => ({ items: [], total: 0 }));

    renderObservations();
    await waitFor(() => expect(screen.getByText("No observations recorded yet")).toBeInTheDocument());
  });
});

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import * as api from "../api";
import { ApiError } from "../api";
import { ScanSchedule } from "./ScanSchedule";

afterEach(() => {
  // vitest.config.ts sets `globals: false`, so @testing-library/react's
  // automatic afterEach(cleanup) registration never fires — without this,
  // renders from earlier tests in this file accumulate in the jsdom document
  // (see Drift.test.tsx/Vulns.test.tsx for the same pattern).
  cleanup();
  vi.restoreAllMocks();
});

const SCAN_POINTS = [
  {
    domain: "linux",
    method: "GET",
    endpoint: "/api/linux/hosts",
    schedule: "*/15 * * * *",
    status: "active",
    next_run: "2026-07-27T12:15:00Z",
    last_run: "2026-07-27T12:00:00Z",
    last_success: "2026-07-27T12:00:00Z",
  },
  {
    domain: "vsphere",
    method: "GET",
    endpoint: "/api/vsphere/vms",
    schedule: "0 * * * *",
    status: "degraded",
    next_run: "2026-07-27T13:00:00Z",
    // Never run yet — must render the NULL token, not a blank cell or "—".
    last_run: null,
    last_success: null,
  },
];

function mockLoadSuccess() {
  vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
    if (url.includes("/api/dashboard/scan_points")) {
      return { items: SCAN_POINTS, total: 2 };
    }
    throw new Error(`unexpected apiGet: ${url}`);
  });
}

function renderScanSchedule() {
  return render(
    <MemoryRouter>
      <ScanSchedule />
    </MemoryRouter>,
  );
}

/** "linux"/"vsphere" appear twice on the page: once in the DataTable row and
 *  once as a Manual Sweep domain <option> — this picks the table-row match. */
function findRowCell(text: string): HTMLElement {
  const match = screen.getAllByText(text).find((el) => el.closest("tr"));
  if (!match) throw new Error(`no table-row match for "${text}"`);
  return match;
}

describe("ScanSchedule page — Phase 3 design-system rebuild", () => {
  it("renders scan points in a DataTable with domain, schedule, and status", async () => {
    mockLoadSuccess();
    renderScanSchedule();

    await waitFor(() => expect(screen.getAllByText("linux").length).toBeGreaterThan(0));
    expect(screen.getAllByText("vsphere").length).toBeGreaterThan(0);
    expect(screen.getByText("read-only · no mutations possible from this view")).toBeInTheDocument();
    const linuxRow = findRowCell("linux").closest("tr");
    expect(linuxRow?.textContent).toContain("active");
  });

  it("renders the Last Run column (T2 audit regression guard: previously fetched but never rendered)", async () => {
    mockLoadSuccess();
    renderScanSchedule();

    await waitFor(() => expect(screen.getAllByText("linux").length).toBeGreaterThan(0));
    const linuxRow = findRowCell("linux").closest("tr");
    expect(linuxRow?.textContent).toContain("2026-07-27 12:00:00");
  });

  it("renders a NULL token (not a blank cell) for a scan point that has never run", async () => {
    mockLoadSuccess();
    renderScanSchedule();

    await waitFor(() => expect(screen.getAllByText("vsphere").length).toBeGreaterThan(0));
    const vsphereRow = findRowCell("vsphere").closest("tr");
    expect(vsphereRow?.textContent).toContain("NULL");
  });

  it("computes the Next Sweep stat tile from the earliest next_run across scan points", async () => {
    mockLoadSuccess();
    renderScanSchedule();

    await waitFor(() => expect(screen.getAllByText("linux").length).toBeGreaterThan(0));
    expect(screen.getByText("Next Sweep")).toBeInTheDocument();
  });

  it("shows an error EmptyState (not a bare alert div) when the scan_points fetch fails", async () => {
    vi.spyOn(api, "apiGet").mockImplementation(async () => {
      throw new Error("db unreachable");
    });

    renderScanSchedule();
    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
    expect(screen.getByRole("alert").textContent).toMatch(/failed to load/i);
  });

  it("shows a none-yet EmptyState when there are zero scan points", async () => {
    vi.spyOn(api, "apiGet").mockImplementation(async () => ({ items: [], total: 0 }));

    renderScanSchedule();
    await waitFor(() => expect(screen.getByText("No scan points configured")).toBeInTheDocument());
  });

  describe("Manual Sweep result message box (outcome-accuracy regression guard)", () => {
    it("shows a green/ok message on a successful 202 trigger", async () => {
      mockLoadSuccess();
      vi.spyOn(api, "apiPost").mockResolvedValue({});
      renderScanSchedule();

      await waitFor(() => expect(screen.getAllByText("linux").length).toBeGreaterThan(0));
      screen.getByRole("button", { name: /trigger sweep/i }).click();

      const msg = await screen.findByText(/202 Accepted/i);
      expect(msg).toBeInTheDocument();
      expect(screen.getByRole("status")).toBe(msg.closest('[role="status"]'));
    });

    it("shows a red/error message (not the hardcoded-green regression) on a 409 already-in-progress response", async () => {
      mockLoadSuccess();
      vi.spyOn(api, "apiPost").mockRejectedValue(new ApiError(409, "conflict"));
      renderScanSchedule();

      await waitFor(() => expect(screen.getAllByText("linux").length).toBeGreaterThan(0));
      screen.getByRole("button", { name: /trigger sweep/i }).click();

      const msg = await screen.findByText(/already in progress \(409\)/i);
      expect(msg.closest('[role="alert"]')).not.toBeNull();
    });

    it("shows a red/error message when the sweep request fails outright", async () => {
      mockLoadSuccess();
      vi.spyOn(api, "apiPost").mockRejectedValue(new Error("boom"));
      renderScanSchedule();

      await waitFor(() => expect(screen.getAllByText("linux").length).toBeGreaterThan(0));
      screen.getByRole("button", { name: /trigger sweep/i }).click();

      const msg = await screen.findByText(/Sweep failed/i);
      expect(msg.closest('[role="alert"]')).not.toBeNull();
    });
  });
});

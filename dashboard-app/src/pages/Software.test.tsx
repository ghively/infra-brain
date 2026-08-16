import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import * as api from "../api";
import { Software } from "./Software";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function aggRow(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    product: "OpenSSL",
    version: "3.0.2",
    vendor: "OpenSSL Software Foundation",
    host_count: 42,
    ...overrides,
  };
}

function detailRow(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    id: "sw-1",
    r7_asset_id: 100,
    hostname: "web01.example.com",
    product: "OpenSSL",
    version: "3.0.2",
    vendor: "OpenSSL Software Foundation",
    software_type: "library",
    ...overrides,
  };
}

function mockLoad(opts?: {
  aggItems?: Array<Record<string, unknown>>;
  detailItems?: Array<Record<string, unknown>>;
  total?: number;
  summary?: Record<string, unknown>;
}) {
  vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
    const isDetail = url.includes("view=detail");
    return {
      view: isDetail ? "detail" : "aggregated",
      items: isDetail ? (opts?.detailItems ?? [detailRow()]) : (opts?.aggItems ?? [aggRow()]),
      total: opts?.total ?? 1,
      limit: 50,
      offset: 0,
      summary: opts?.summary ?? { total_records: 335000, unique_products: 812, hosts_covered: 640 },
    };
  });
}

function renderSoftware() {
  return render(
    <MemoryRouter>
      <Software />
    </MemoryRouter>,
  );
}

describe("Software page — Phase 3 design-system rebuild", () => {
  it("renders the aggregated view by default with stat tiles and the software table", async () => {
    mockLoad();

    renderSoftware();

    await waitFor(() => expect(screen.getByText("OpenSSL")).toBeInTheDocument());
    expect(screen.getByText("Total Records")).toBeInTheDocument();
    expect(screen.getByText("335000")).toBeInTheDocument();
    expect(screen.getByText("Unique Products")).toBeInTheDocument();
    expect(screen.getByText("Hosts Covered")).toBeInTheDocument();
    expect(screen.getByText("42")).toBeInTheDocument();
  });

  it("switching to Per-host detail re-fetches with view=detail and shows hostname column", async () => {
    mockLoad();

    renderSoftware();

    await waitFor(() => expect(screen.getByText("OpenSSL")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("tab", { name: "Per-host detail" }));

    await waitFor(() => expect(screen.getByText("web01.example.com")).toBeInTheDocument());
  });

  it("shows a not-configured empty state when no software records exist and no search is active", async () => {
    mockLoad({ aggItems: [], total: 0 });

    renderSoftware();

    await waitFor(() =>
      expect(screen.getByText("No software inventory is being collected yet")).toBeInTheDocument(),
    );
    // The copy must name the REAL state of play: the page is kept deliberately,
    // Rapid7's collector is retired, and the Linux package path exists in the
    // schema but is gated behind fleet SSH. Asserting the Rapid7 env-var advice
    // here would re-assert the pre-2026-08-13 framing.
    expect(screen.getByText(/linux_packages/)).toBeInTheDocument();
    expect(
      screen.getByText("No software inventory is being collected yet").closest("[data-empty-kind]"),
    ).toHaveAttribute("data-empty-kind", "not-configured");
  });

  it("shows only the loading skeleton — not a zero-row table or stat tiles — while the initial fetch is in flight", () => {
    mockLoad();

    renderSoftware();

    // Synchronously right after mount, before the mocked apiGet promise has
    // resolved, both detailItems and aggItems are still null (no data ever
    // loaded). The page must show only the loading skeleton here, matching
    // every sibling list page's `data === null` early return — not the
    // stat-tile grid, tabs, search box, or a DataTable with zero rows
    // rendered underneath a second, separate skeleton.
    expect(screen.queryByText("Total Records")).not.toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "Aggregated" })).not.toBeInTheDocument();
    expect(screen.queryByText("software (aggregated)")).not.toBeInTheDocument();
    expect(screen.queryByText("Software inventory is not configured")).not.toBeInTheDocument();
  });

  it("shows a full-page error state on load failure", async () => {
    vi.spyOn(api, "apiGet").mockRejectedValue(new Error("network unreachable"));

    renderSoftware();

    await waitFor(() =>
      expect(screen.getByText("Software inventory failed to load")).toBeInTheDocument(),
    );
  });
});

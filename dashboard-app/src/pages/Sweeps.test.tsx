import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import * as api from "../api";
import { Sweeps } from "./Sweeps";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function renderSweeps() {
  return render(
    <MemoryRouter>
      <Sweeps />
    </MemoryRouter>,
  );
}

const ZERO_COUNTS = {
  completed: 0,
  partial: 0,
  failed: 0,
  retry_exhausted: 0,
  interrupt_pending: 0,
  in_progress: 0,
  skipped: 0,
};

const BASE_SWEEP = {
  sweep_id: "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
  started_at: "2026-07-27T00:00:00Z",
  finished_at: "2026-07-27T00:05:00Z",
  duration_seconds: 300,
  domain_count: 3,
  status_counts: { ...ZERO_COUNTS, completed: 3 },
  max_iters_hit_count: 0,
};

function mockSweeps(items: (typeof BASE_SWEEP)[], detailDomains: unknown[] = []) {
  vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
    if (url.includes("/sweeps/")) {
      return {
        sweep_id: items[0]?.sweep_id ?? "x",
        started_at: items[0]?.started_at ?? null,
        finished_at: items[0]?.finished_at ?? null,
        duration_seconds: items[0]?.duration_seconds ?? null,
        status_counts: items[0]?.status_counts ?? ZERO_COUNTS,
        max_iters_hit_count: items[0]?.max_iters_hit_count ?? 0,
        domains: detailDomains,
      };
    }
    return { items, total: items.length, limit: 20 };
  });
}

describe("Sweeps page", () => {
  it("renders the sweep list and auto-selects the first sweep's detail", async () => {
    mockSweeps([BASE_SWEEP], [
      { domain: "linux", tier: "collector", status: "completed", started_at: null, finished_at: null, duration_seconds: 10, records_collected: 5, error_message: null, max_iters_hits: 0 },
    ]);

    renderSweeps();
    await waitFor(() => expect(screen.getAllByText(/aaaaaaaa/).length).toBeGreaterThan(0));
    await waitFor(() => expect(screen.getByText("linux")).toBeInTheDocument());
    expect(screen.getByText("COLLECTORS")).toBeInTheDocument();
  });

  it("does not blank the page on a fetch error", async () => {
    vi.spyOn(api, "apiGet").mockRejectedValue(new Error("network unreachable"));

    renderSweeps();
    await waitFor(() => expect(screen.getByText(/failed to load/i)).toBeInTheDocument());
  });

  it("status-count chips include partial and skipped (not silently dropped) and the row never disappears", async () => {
    const sweep = {
      ...BASE_SWEEP,
      sweep_id: "11111111-2222-3333-4444-555555555555",
      domain_count: 5,
      status_counts: { ...ZERO_COUNTS, completed: 2, partial: 1, skipped: 2 },
    };
    mockSweeps([sweep]);

    renderSweeps();
    await waitFor(() => expect(screen.getAllByText(/11111111/).length).toBeGreaterThan(0));
    expect(screen.getByText("done 2")).toBeInTheDocument();
    expect(screen.getByText("partial 1")).toBeInTheDocument();
    expect(screen.getByText("skipped 2")).toBeInTheDocument();
  });

  it("counts retry_exhausted toward the page's failed total in the header pill", async () => {
    const sweep = {
      ...BASE_SWEEP,
      sweep_id: "99999999-8888-7777-6666-555555555555",
      status_counts: { ...ZERO_COUNTS, completed: 1, retry_exhausted: 2 },
    };
    mockSweeps([sweep]);

    renderSweeps();
    await waitFor(() => expect(screen.getAllByText(/99999999/).length).toBeGreaterThan(0));
    expect(screen.getByText("2 failed")).toBeInTheDocument();
    expect(screen.getByText("retry-exh 2")).toBeInTheDocument();
  });

  it("clicking a different sweep row loads its own detail", async () => {
    const sweepA = { ...BASE_SWEEP, sweep_id: "aaaaaaaa-0000-0000-0000-000000000000" };
    const sweepB = { ...BASE_SWEEP, sweep_id: "bbbbbbbb-0000-0000-0000-000000000000" };
    const seen: string[] = [];
    vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
      seen.push(url);
      if (url.includes(sweepB.sweep_id)) {
        return {
          ...sweepB,
          domains: [{ domain: "windows", tier: "reconciler", status: "completed", started_at: null, finished_at: null, duration_seconds: 5, records_collected: 1, error_message: null, max_iters_hits: 0 }],
        };
      }
      if (url.includes("/sweeps/")) {
        return {
          ...sweepA,
          domains: [{ domain: "linux", tier: "collector", status: "completed", started_at: null, finished_at: null, duration_seconds: 5, records_collected: 1, error_message: null, max_iters_hits: 0 }],
        };
      }
      return { items: [sweepA, sweepB], total: 2, limit: 20 };
    });

    renderSweeps();
    await waitFor(() => expect(screen.getByText("linux")).toBeInTheDocument());

    screen.getByText(/bbbbbbbb/).click();
    await waitFor(() => expect(screen.getByText("windows")).toBeInTheDocument());
  });

  it("shows a not-configured empty state (not a bare 'no data yet') when zero sweeps exist", async () => {
    // sweep_id is only ever minted by the sweep-graph orchestrator
    // (sweep_graph_enabled, off by default) — an empty list here is the
    // expected steady state on a stock install, not a sign nothing has run.
    mockSweeps([]);

    renderSweeps();
    await waitFor(() => expect(screen.getAllByText("No sweep-graph runs recorded").length).toBeGreaterThan(0));
    const [emptyState] = screen.getAllByText("No sweep-graph runs recorded");
    expect(emptyState.closest("[data-empty-kind]")).toHaveAttribute("data-empty-kind", "not-configured");
    expect(screen.getAllByText(/sweep_graph_enabled/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/Collection Runs has the same per-domain run history/).length).toBeGreaterThan(0);
  });
});

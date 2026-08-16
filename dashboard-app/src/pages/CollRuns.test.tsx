import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import * as api from "../api";
import { CollRuns } from "./CollRuns";

afterEach(() => {
  // vitest.config.ts sets `globals: false`, so @testing-library/react's
  // automatic afterEach(cleanup) registration never fires — without this,
  // renders from earlier tests in this file accumulate in the jsdom document
  // (same pattern as Drift.test.tsx/Home.test.tsx).
  cleanup();
  vi.restoreAllMocks();
});

function renderCollRuns() {
  return render(
    <MemoryRouter>
      <CollRuns />
    </MemoryRouter>,
  );
}

const BASE_RUN = {
  domain: "linux",
  trigger_type: "schedule",
  status: "success",
  records_collected: 12,
  started_at: "2026-07-27T00:00:00Z",
  duration_seconds: 4,
  error_message: null,
};

describe("CollRuns page", () => {
  it("renders the runs table on a successful load", async () => {
    vi.spyOn(api, "apiGet").mockResolvedValue({
      items: [BASE_RUN],
      total: 1,
    });

    renderCollRuns();
    // "linux" is also a domain-filter <option>, so match all and confirm at
    // least the table cell rendered.
    await waitFor(() => expect(screen.getAllByText("linux").length).toBeGreaterThan(0));
    expect(screen.getByText("success")).toBeInTheDocument();
  });

  it("does not blank the page on a fetch error — filter tabs stay visible", async () => {
    vi.spyOn(api, "apiGet").mockRejectedValue(new Error("network unreachable"));

    renderCollRuns();
    await waitFor(() => expect(screen.getByText(/failed to load/i)).toBeInTheDocument());
  });

  it("broadens partial-run error detection: a partial run with an error_message shows the error snippet and drawer error block", async () => {
    vi.spyOn(api, "apiGet").mockResolvedValue({
      items: [
        {
          ...BASE_RUN,
          domain: "windows",
          status: "partial",
          error_message: "collector timed out after 300s waiting on WinRM handshake to complete for host db02",
        },
      ],
      total: 1,
    });

    renderCollRuns();
    await waitFor(() => expect(screen.getAllByText("windows").length).toBeGreaterThan(0));
    expect(screen.getByText("partial")).toBeInTheDocument();

    // 40-char-truncated snippet with ellipsis in the table cell.
    const snippet = "collector timed out after 300s waiting o…";
    expect(screen.getByTitle(/collector timed out after 300s waiting on WinRM/)).toHaveTextContent(snippet);

    // Row click opens the drawer with the full error text and an Error block
    // (RunDetail's hasError check must include "partial", not just "failure").
    screen.getByText("partial").click();
    await waitFor(() => expect(screen.getByText("Error")).toBeInTheDocument());
    expect(
      screen.getByText("collector timed out after 300s waiting on WinRM handshake to complete for host db02"),
    ).toBeInTheDocument();
  });

  it("status filter tabs include Partial and filter the table", async () => {
    // F-5: status is now pushed server-side (fleet.py::list_runs `status=`),
    // so the mock must actually honor it rather than always returning the
    // same fixed rows regardless of URL.
    const ALL_ROWS = [BASE_RUN, { ...BASE_RUN, domain: "windows", status: "partial", error_message: "degraded" }];
    vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
      if (url.includes("/api/dashboard/agents")) {
        return { items: [{ domain: "linux" }, { domain: "windows" }], total: 2 };
      }
      const params = new URLSearchParams(url.split("?")[1] ?? "");
      const statusParam = params.get("status");
      const matched = statusParam ? ALL_ROWS.filter((r) => r.status === statusParam) : ALL_ROWS;
      return { items: matched, total: matched.length };
    });

    renderCollRuns();
    await waitFor(() => expect(screen.getAllByText("linux").length).toBeGreaterThan(0));
    expect(screen.getAllByText("windows").length).toBeGreaterThan(0);

    screen.getByText("Partial").click();
    // Once filtered, "linux" only remains in the (unaffected) domain <select>
    // options, never in a table row — assert the success status pill is gone.
    await waitFor(() => expect(screen.queryByText("success")).not.toBeInTheDocument());
    expect(screen.getByText("partial")).toBeInTheDocument();
  });

  it("F-5: pushes the domain filter server-side so a domain outside the fetched window is not falsely reported as no matches", async () => {
    // Simulate the exact failure mode: the unfiltered 100-most-recent window
    // is all "linux" runs (the "windows" domain has not run recently enough
    // to be in that window), but the backend DOES have matching "windows"
    // rows and returns them once the filter is actually sent as a query
    // param.
    vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
      if (url.includes("/api/dashboard/agents")) {
        return { items: [{ domain: "linux" }, { domain: "windows" }], total: 2 };
      }
      if (url.includes("domain=windows")) {
        return { items: [{ ...BASE_RUN, domain: "windows", status: "partial", error_message: "old run" }], total: 1 };
      }
      // Unfiltered fetch: only "linux" rows in the truncated recent window.
      return { items: [BASE_RUN], total: 1 };
    });

    renderCollRuns();
    await waitFor(() => expect(screen.getAllByText("linux").length).toBeGreaterThan(0));

    const select = screen.getByDisplayValue("(all)");
    fireEvent.change(select, { target: { value: "windows" } });

    // A real server-side-filtered result must appear (its distinct "partial"
    // status badge only renders from an actual table row, never from the
    // domain <option> alone) -- not a false "no matches" derived from
    // client-filtering the stale unfiltered window.
    await waitFor(() => expect(screen.getByText("partial")).toBeInTheDocument());
    expect(screen.queryByText("No collection runs match the current filters.")).not.toBeInTheDocument();
  });

  it("treats retry_exhausted as a failure-family status via the Badge tone (no crash, distinguishable text)", async () => {
    vi.spyOn(api, "apiGet").mockResolvedValue({
      items: [{ ...BASE_RUN, domain: "vsphere", status: "retry_exhausted", error_message: "gave up after 5 retries" }],
      total: 1,
    });

    renderCollRuns();
    await waitFor(() => expect(screen.getAllByText("vsphere").length).toBeGreaterThan(0));
    expect(screen.getByText("retry_exhausted")).toBeInTheDocument();
  });
});

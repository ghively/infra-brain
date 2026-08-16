import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import * as api from "../api";
import { Graph } from "./Graph";

afterEach(() => {
  // vitest.config.ts sets `globals: false`, so @testing-library/react's
  // automatic afterEach(cleanup) registration never fires — without this,
  // renders from earlier tests in this file accumulate in the jsdom document
  // (see Security.test.tsx/Drift.test.tsx for the same pattern).
  cleanup();
  vi.restoreAllMocks();
});

/** Graph-first P5: there is ONE store. The legacy `resource_relationships`
 *  surface this file used to exercise (/api/graph/stats' legacy body,
 *  /relationships, /search, /{resource_id}) is gone, along with the store
 *  toggle that reached it and the "frozen, removal planned" notice that warned
 *  about it. Those tests are not ported — a warning banner's correct end state
 *  is the view not existing, so what replaces them is the assertion that the
 *  toggle and the notice are absent.
 *
 *  What stays here is the chrome that was never store-specific: the loading
 *  skeleton, the empty states, the SVG remount regression, and the Review
 *  Queue tab. Store behaviour lives in Graph.kg.test.tsx. */

const KG_STATS = {
  node_types: [{ type: "LinuxHost", count: 3 }],
  edge_types: [{ type: "RUNS_ON", count: 42 }],
  total_nodes: 3,
  total_edges: 42,
  active_only: true,
};

const EMPTY_KG_STATS = {
  node_types: [],
  edge_types: [],
  total_nodes: 0,
  total_edges: 0,
  active_only: true,
};

function renderGraph() {
  render(
    <MemoryRouter>
      <Graph />
    </MemoryRouter>,
  );
}

describe("Graph page loading-state consistency (#91 secondary finding)", () => {
  it("shows a Skeleton placeholder while stats are loading, not silently nothing", async () => {
    // Never-resolving promise — keeps the loading flag true for the assertion window.
    vi.spyOn(api, "apiGet").mockImplementation(() => new Promise(() => {}));
    renderGraph();
    // The Skeleton component renders divs with the ib-skeleton-pulse animation
    // class inline style — assert via a stable, purpose-built test id instead
    // of matching on style internals.
    expect(await screen.findByTestId("graph-kg-stats-skeleton")).toBeInTheDocument();
  });
});

describe("Graph page — the legacy store surface is gone (P5)", () => {
  it("offers no store toggle and never calls a removed endpoint", async () => {
    const spy = vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
      if (url.includes("/api/graph/kg/stats")) return KG_STATS;
      throw new Error(`unexpected apiGet: ${url}`);
    });
    renderGraph();
    await screen.findByTestId("graph-legend");

    expect(screen.queryByRole("button", { name: /resource_relationships/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /^Legacy/i })).toBeNull();
    // The route each of those buttons drove is removed server-side; the page
    // must not be reaching for any of them on mount either.
    const called = spy.mock.calls.map(([u]) => String(u));
    expect(called.some((u) => /\/api\/graph\/(relationships|search)/.test(u))).toBe(false);
    expect(called.every((u) => u.includes("/api/graph/kg/") || !u.startsWith("/api/graph/"))).toBe(
      true,
    );
  });

  it("shows no frozen-store notice, because there is no frozen store to warn about", async () => {
    vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
      if (url.includes("/api/graph/kg/stats")) return KG_STATS;
      throw new Error(`unexpected apiGet: ${url}`);
    });
    renderGraph();
    await screen.findByTestId("graph-legend");
    expect(screen.queryByTestId("graph-legacy-frozen-notice")).not.toBeInTheDocument();
    expect(screen.queryByText(/removal planned/i)).toBeNull();
  });
});

describe("Graph page — Phase 3 design-system rebuild", () => {
  function mockKg(overrides: Record<string, unknown> = {}) {
    return vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
      for (const [fragment, value] of Object.entries(overrides)) {
        if (url.includes(fragment)) return value;
      }
      if (url.includes("/api/graph/kg/stats")) return KG_STATS;
      if (url.includes("/api/graph/kg/search")) return { candidates: [], total: 0, limit: 25 };
      if (url.includes("/api/graph/entity-resolution/queue")) return { items: [], total: 0 };
      throw new Error(`unexpected apiGet: ${url}`);
    });
  }

  it("renders the edge-type chip bar from /api/graph/kg/stats", async () => {
    mockKg();
    renderGraph();
    await waitFor(() => expect(screen.getAllByText(/RUNS_ON/).length).toBeGreaterThan(0));
  });

  it("shows a none-yet empty state inside the canvas before any search has run (TRK-238 item 2)", async () => {
    mockKg();
    renderGraph();

    // Previously the bordered canvas rendered with nothing inside it until a
    // search was performed — a blank box with no explanation. It should now
    // explain there's nothing to show yet and prompt the user to search.
    expect(await screen.findByText("No graph data to show yet")).toBeInTheDocument();
    expect(screen.getByText(/Search above for a host/)).toBeInTheDocument();
  });

  it("shows a filter-zero empty state when a search returns no nodes", async () => {
    mockKg({ "/api/graph/kg/stats": EMPTY_KG_STATS });
    renderGraph();
    await screen.findByTestId("graph-legend");

    fireEvent.change(screen.getByPlaceholderText("Search host, IP, or resource name..."), {
      target: { value: "nonexistent-host" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));

    expect(await screen.findByText("No results")).toBeInTheDocument();
  });

  it("repopulates the SVG canvas after leaving Explorer for Review Queue and returning (regression)", async () => {
    // Bug: the <svg ref={svgRef}> lives inside the `activeTab === "explorer"`
    // subtree, so switching to Review Queue unmounts it and switching back
    // mounts a brand-new, empty svg. The render effect was keyed only on the
    // data (which doesn't change on a tab flip), so the fresh svg never got
    // repopulated even though the last search result was still held in state.
    const NODE_ID = "11111111-1111-1111-1111-111111111111";
    mockKg({
      "/api/graph/kg/search": {
        candidates: [
          {
            id: NODE_ID,
            type: "LinuxHost",
            name: "web01",
            natural_key: "web01",
            source: "linux",
            resource_id: null,
            attributes: {},
            first_seen: null,
            last_seen: null,
          },
        ],
        total: 1,
        limit: 25,
      },
      [`/api/graph/kg/${NODE_ID}`]: {
        root_id: NODE_ID,
        nodes: [
          {
            id: NODE_ID,
            type: "LinuxHost",
            name: "web01",
            natural_key: "web01",
            source: "linux",
            resource_id: null,
            attributes: {},
            first_seen: null,
            last_seen: null,
          },
        ],
        edges: [],
        node_total: 1,
        edge_total: 0,
        truncated: false,
        depth: 2,
        active_only: true,
        walk_ceiling_hit: false,
      },
    });

    renderGraph();
    await screen.findByTestId("graph-legend");

    fireEvent.change(screen.getByPlaceholderText("Search host, IP, or resource name..."), {
      target: { value: "web01" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));

    // Search result painted onto the canvas. Scoped to the graph canvas's own
    // svg (data-testid="graph-canvas-svg") rather than a bare "svg circle"
    // selector — several icons elsewhere on the page (e.g. the page header
    // icon) are also <svg><circle .../></svg>, which would false-positive.
    await waitFor(() =>
      expect(screen.getByTestId("graph-canvas-svg").querySelector("circle")).not.toBeNull(),
    );

    // Away to Review Queue (unmounts the svg) ...
    fireEvent.click(screen.getByRole("tab", { name: "Review Queue" }));
    await waitFor(() => expect(screen.getByText("Nothing to review")).toBeInTheDocument());

    // ... and back to Explorer (mounts a fresh, empty svg).
    fireEvent.click(screen.getByRole("tab", { name: "Explorer" }));

    // The still-held result from the earlier search must repaint onto the
    // newly-mounted svg without requiring another search.
    await waitFor(() =>
      expect(screen.getByTestId("graph-canvas-svg").querySelector("circle")).not.toBeNull(),
    );
  });
});

describe("Graph page — Review Queue tab (TRK-226)", () => {
  const oneQueueRow = {
    action_id: "aaaaaaaa-1111-1111-1111-111111111111",
    source_node: {
      node_id: "src-node-1",
      node_type: "VsphereVM",
      natural_key: "vsphere:web01",
      name: "web01",
      source: "vsphere",
    },
    candidate_matches: [
      {
        node_id: "cand-node-1",
        node_type: "R7Asset",
        natural_key: "r7:web01-prod",
        name: "web01-prod",
        source: "rapid7",
        score: 0.87,
        reason: "fuzzy name match",
      },
    ],
    status: "pending",
    best_score: 0.87,
    approved_by: null,
    created_at: "2026-07-28T00:00:00Z",
    retraction_history: [],
  };

  function mockGraphAndQueue(items: unknown[]) {
    vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
      if (url.includes("/api/graph/kg/stats")) return EMPTY_KG_STATS;
      if (url.includes("/api/graph/entity-resolution/queue")) return { items, total: items.length };
      throw new Error(`unexpected apiGet: ${url}`);
    });
  }

  it("does not fetch the review queue while the Explorer tab is active", async () => {
    mockGraphAndQueue([oneQueueRow]);

    render(
      <MemoryRouter>
        <Graph />
      </MemoryRouter>,
    );

    await waitFor(() =>
      expect(api.apiGet).toHaveBeenCalledWith(expect.stringContaining("/api/graph/kg/stats")),
    );
    expect(api.apiGet).not.toHaveBeenCalledWith(expect.stringContaining("/entity-resolution/queue"));
  });

  it("shows a none-yet empty state when the review queue is empty", async () => {
    mockGraphAndQueue([]);

    render(
      <MemoryRouter>
        <Graph />
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByRole("tab", { name: "Review Queue" }));

    expect(await screen.findByText("Nothing to review")).toBeInTheDocument();
  });

  it("renders a pending row with its source node and candidate match", async () => {
    mockGraphAndQueue([oneQueueRow]);

    render(
      <MemoryRouter>
        <Graph />
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByRole("tab", { name: "Review Queue" }));

    expect(await screen.findByText("web01")).toBeInTheDocument();
    expect(screen.getByText(/web01-prod/)).toBeInTheDocument();
    expect(screen.getByText(/score 0.870/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Confirm match" })).toBeInTheDocument();
  });

  it("confirming a candidate posts to /entity-resolution/{id}/confirm with the candidate's node_id", async () => {
    mockGraphAndQueue([oneQueueRow]);
    const postSpy = vi.spyOn(api, "apiPost").mockResolvedValue({ confirmed: true });

    render(
      <MemoryRouter>
        <Graph />
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByRole("tab", { name: "Review Queue" }));
    fireEvent.click(await screen.findByRole("button", { name: "Confirm match" }));

    await waitFor(() =>
      expect(postSpy).toHaveBeenCalledWith(
        `/api/graph/entity-resolution/${oneQueueRow.action_id}/confirm`,
        { target_node_id: "cand-node-1" },
      ),
    );
    expect(await screen.findByText(/Confirmed as the same entity/)).toBeInTheDocument();
  });

  it("rejecting requires a second click (confirm-then-reject) and posts the generic reject endpoint", async () => {
    mockGraphAndQueue([oneQueueRow]);
    const postSpy = vi.spyOn(api, "apiPost").mockResolvedValue({ rejected: true });

    render(
      <MemoryRouter>
        <Graph />
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByRole("tab", { name: "Review Queue" }));
    const rejectBtn = await screen.findByRole("button", { name: /Reject/ });
    fireEvent.click(rejectBtn);
    expect(postSpy).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Click again to confirm reject" }));

    await waitFor(() =>
      expect(postSpy).toHaveBeenCalledWith(
        `/api/dashboard/actions/${oneQueueRow.action_id}/reject`,
      ),
    );
  });

  const approvedQueueRow = {
    ...oneQueueRow,
    action_id: "bbbbbbbb-2222-2222-2222-222222222222",
    status: "approved",
    approved_by: "operator",
  };

  it("shows a Retract button (not Confirm match) on an already-approved row", async () => {
    mockGraphAndQueue([approvedQueueRow]);

    render(
      <MemoryRouter>
        <Graph />
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByRole("tab", { name: "Review Queue" }));

    expect(await screen.findByText(/Retract/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Confirm match" })).not.toBeInTheDocument();
    expect(screen.getByText("Resolved by operator")).toBeInTheDocument();
  });

  it("retracting requires a second click (confirm-then-retract) and posts the retract endpoint", async () => {
    mockGraphAndQueue([approvedQueueRow]);
    const postSpy = vi.spyOn(api, "apiPost").mockResolvedValue({ retracted: true });

    render(
      <MemoryRouter>
        <Graph />
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByRole("tab", { name: "Review Queue" }));
    const retractBtn = await screen.findByRole("button", { name: /Retract/ });
    fireEvent.click(retractBtn);
    expect(postSpy).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Click again to confirm retract" }));

    await waitFor(() =>
      expect(postSpy).toHaveBeenCalledWith(
        `/api/graph/entity-resolution/${approvedQueueRow.action_id}/retract`,
        { reason: "Retracted from the dashboard review queue." },
      ),
    );
    expect(await screen.findByText(/Retracted — the edge was undone/)).toBeInTheDocument();
  });

  it("surfaces a retract failure instead of silently leaving the row unchanged", async () => {
    mockGraphAndQueue([approvedQueueRow]);
    vi.spyOn(api, "apiPost").mockRejectedValue(new Error("action is pending, not approved"));

    render(
      <MemoryRouter>
        <Graph />
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByRole("tab", { name: "Review Queue" }));
    fireEvent.click(await screen.findByRole("button", { name: /Retract/ }));
    fireEvent.click(screen.getByRole("button", { name: "Click again to confirm retract" }));

    expect(await screen.findByText(/action is pending, not approved/)).toBeInTheDocument();
  });

  it("shows a 'previously confirmed and retracted' marker when a reopened row has retraction history", async () => {
    const reopenedRow = {
      ...oneQueueRow,
      action_id: "cccccccc-3333-3333-3333-333333333333",
      status: "pending",
      retraction_history: [
        { approved_by: "operator", retracted_by: "alice", retracted_at: "2026-07-28T00:00:00Z", reason: "oops" },
      ],
    };
    mockGraphAndQueue([reopenedRow]);

    render(
      <MemoryRouter>
        <Graph />
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByRole("tab", { name: "Review Queue" }));

    expect(await screen.findByText(/Previously confirmed by operator/)).toBeInTheDocument();
    expect(screen.getByText(/retracted by alice/)).toBeInTheDocument();
  });
});

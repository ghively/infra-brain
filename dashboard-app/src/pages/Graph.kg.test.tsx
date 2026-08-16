import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import * as api from "../api";
import { Graph } from "./Graph";

/** Graph page — knowledge-graph store (graph_nodes / graph_edges).
 *
 * The page used to read ONLY `resource_relationships`: /api/graph/stats,
 * /api/graph/relationships, /api/graph/search and /api/graph/{resource_id} all
 * resolved against that table. Every edge the declarative graph_engine writes —
 * RUNS_ON (service→host), BELONGS_TO (iac file→project), DEFINED_IN
 * (project→file) — lives in graph_edges instead, so the flagship view of the
 * product's core could not draw a single one of them.
 *
 * Graph-first P5 finished that migration: those four routes are gone and this
 * is the only store the page reads. The `mockApi` helper below is the
 * mechanical proof — it throws on any unmocked URL and no longer carries a
 * handler for a single legacy endpoint, so a page that reached for one would
 * fail every test in this file rather than one dedicated to it.
 *
 * These tests pin the two headline traversals (host→its services, project→its
 * files), that node type and edge type are both legible, that retired edges
 * are excluded unless asked for, and that a capped result says how much it is
 * not showing.
 */

afterEach(() => {
  // vitest.config.ts sets `globals: false`, so @testing-library/react's
  // automatic afterEach(cleanup) never registers (same pattern as Graph.test.tsx).
  cleanup();
  vi.restoreAllMocks();
});

const HOST_ID = "11111111-1111-1111-1111-111111111111";
const SVC_ID = "22222222-2222-2222-2222-222222222222";
const PROJ_ID = "33333333-3333-3333-3333-333333333333";
const FILE_ID = "44444444-4444-4444-4444-444444444444";

const KG_STATS = {
  node_types: [
    { type: "HomelabService", count: 141 },
    { type: "LinuxHost", count: 12 },
    { type: "IaCFile", count: 118 },
    { type: "GitlabProject", count: 5 },
  ],
  edge_types: [
    { type: "BELONGS_TO", count: 62 },
    { type: "RUNS_ON", count: 34 },
    { type: "DEFINED_IN", count: 22 },
  ],
  total_nodes: 276,
  total_edges: 118,
  active_only: true,
};

function kgNode(over: Partial<Record<string, unknown>> = {}) {
  return {
    id: HOST_ID,
    type: "LinuxHost",
    name: "node_a",
    natural_key: "node_a",
    source: "linux",
    resource_id: null,
    attributes: {},
    first_seen: null,
    last_seen: null,
    ...over,
  };
}

const HOST_NEIGHBOURHOOD = {
  root_id: HOST_ID,
  nodes: [
    kgNode(),
    kgNode({
      id: SVC_ID,
      type: "HomelabService",
      name: "litellm",
      natural_key: "node_a/litellm",
      source: "homelab_services",
      attributes: { host: "node_a", port: 4000 },
    }),
  ],
  edges: [
    {
      id: "e1",
      source_id: SVC_ID,
      target_id: HOST_ID,
      edge_type: "RUNS_ON",
      method: "deterministic_match",
      confidence: 0.99,
      authority: "auto",
      source: "graph_engine.homelab_services",
      evidence: {},
      valid_from: "2026-08-11T00:00:00Z",
      valid_to: null,
    },
  ],
  node_total: 2,
  edge_total: 1,
  truncated: false,
  depth: 2,
  active_only: true,
  walk_ceiling_hit: false,
};

const PROJECT_NEIGHBOURHOOD = {
  ...HOST_NEIGHBOURHOOD,
  root_id: PROJ_ID,
  nodes: [
    kgNode({ id: PROJ_ID, type: "GitlabProject", name: "homelab-ansible", natural_key: "113", source: "cicd" }),
    kgNode({ id: FILE_ID, type: "IaCFile", name: "site.yml", natural_key: "113:site.yml", source: "iac" }),
  ],
  edges: [
    {
      id: "e2",
      source_id: FILE_ID,
      target_id: PROJ_ID,
      edge_type: "BELONGS_TO",
      method: "declared",
      confidence: 1,
      authority: "auto",
      source: "graph_engine.iac",
      evidence: {},
      valid_from: "2026-08-11T00:00:00Z",
      valid_to: null,
    },
    {
      id: "e3",
      source_id: PROJ_ID,
      target_id: FILE_ID,
      edge_type: "DEFINED_IN",
      method: "declared",
      confidence: 1,
      authority: "auto",
      source: "graph_engine.iac",
      evidence: {},
      valid_from: "2026-08-11T00:00:00Z",
      valid_to: null,
    },
  ],
};

/** Mock every endpoint the page may touch. `overrides` replaces a handler by
 *  URL fragment; anything unmocked throws, so an unexpected call is a failure
 *  rather than a silent undefined. */
function mockApi(overrides: Record<string, unknown> = {}) {
  return vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
    for (const [fragment, value] of Object.entries(overrides)) {
      if (url.includes(fragment)) return value;
    }
    if (url.includes("/api/graph/kg/stats")) return KG_STATS;
    if (url.includes("/api/graph/kg/search")) {
      return { candidates: [kgNode()], total: 1, limit: 25 };
    }
    if (url.includes(`/api/graph/kg/${HOST_ID}`)) return HOST_NEIGHBOURHOOD;
    if (url.includes(`/api/graph/kg/${PROJ_ID}`)) return PROJECT_NEIGHBOURHOOD;
    if (url.includes("/api/graph/entity-resolution/queue")) return { items: [], total: 0 };
    throw new Error(`unexpected apiGet: ${url}`);
  });
}

function renderGraph() {
  render(
    <MemoryRouter>
      <Graph />
    </MemoryRouter>,
  );
}

async function search(term: string) {
  fireEvent.change(await screen.findByTestId("graph-search-input"), { target: { value: term } });
  fireEvent.click(screen.getByRole("button", { name: "Search" }));
}

describe("Graph Explorer reads the graph_nodes/graph_edges store by default", () => {
  it("loads /api/graph/kg/stats on mount", async () => {
    const spy = mockApi();
    renderGraph();
    await waitFor(() =>
      expect(spy.mock.calls.some(([url]) => String(url).includes("/api/graph/kg/stats"))).toBe(true),
    );
  });

  it("renders the three graph_engine edge types with their counts", async () => {
    mockApi();
    renderGraph();
    const legend = await screen.findByTestId("graph-legend");
    await waitFor(() => expect(within(legend).getByText(/RUNS_ON/)).toBeInTheDocument());
    expect(within(legend).getByText(/BELONGS_TO/)).toBeInTheDocument();
    expect(within(legend).getByText(/DEFINED_IN/)).toBeInTheDocument();
  });

  it("renders node TYPES in the legend, not resource domains", async () => {
    mockApi();
    renderGraph();
    const legend = await screen.findByTestId("graph-legend");
    await waitFor(() => expect(within(legend).getByText(/HomelabService/)).toBeInTheDocument());
    expect(within(legend).getByText(/LinuxHost/)).toBeInTheDocument();
    expect(within(legend).getByText(/GitlabProject/)).toBeInTheDocument();
  });

  it("states how big the whole store is, so the page is honest about scale", async () => {
    mockApi();
    renderGraph();
    expect(await screen.findByTestId("graph-store-summary")).toHaveTextContent(/276/);
    expect(screen.getByTestId("graph-store-summary")).toHaveTextContent(/118/);
  });
});

describe("Graph Explorer — the two headline traversals", () => {
  it("pick a host, see its services", async () => {
    const spy = mockApi();
    renderGraph();
    await screen.findByTestId("graph-legend");

    await search("node_a");

    // One match auto-opens its neighbourhood rather than making the user click
    // a single-item candidate list.
    await waitFor(() =>
      expect(spy.mock.calls.some(([url]) => String(url).includes(`/api/graph/kg/${HOST_ID}`))).toBe(true),
    );
    const overlay = await screen.findByTestId("graph-canvas-counts");
    await waitFor(() => expect(overlay).toHaveTextContent(/2 nodes/));
    expect(screen.getByTestId("graph-canvas-svg").querySelector("circle")).not.toBeNull();
  });

  it("pick a project, see its files, with both edge directions", async () => {
    mockApi({
      "/api/graph/kg/search": {
        candidates: [kgNode({ id: PROJ_ID, type: "GitlabProject", name: "homelab-ansible", natural_key: "113" })],
        total: 1,
        limit: 25,
      },
    });
    renderGraph();
    await screen.findByTestId("graph-legend");

    await search("homelab-ansible");

    const svg = await screen.findByTestId("graph-canvas-svg");
    await waitFor(() => expect(svg.textContent).toContain("BELONGS_TO"));
    expect(svg.textContent).toContain("DEFINED_IN");
  });

  it("offers a disambiguation list when a search matches more than one node", async () => {
    mockApi({
      "/api/graph/kg/search": {
        candidates: [
          kgNode({ id: HOST_ID, name: "node_a" }),
          kgNode({ id: SVC_ID, type: "HomelabService", name: "node_a/litellm", natural_key: "node_a/litellm" }),
        ],
        total: 2,
        limit: 25,
      },
    });
    renderGraph();
    await screen.findByTestId("graph-legend");
    await search("node_a");
    expect(await screen.findByText(/Multiple matches/)).toBeInTheDocument();
  });

  it("shows a filter-zero empty state when nothing matches", async () => {
    mockApi({ "/api/graph/kg/search": { candidates: [], total: 0, limit: 25 } });
    renderGraph();
    await screen.findByTestId("graph-legend");
    await search("no-such-thing");
    expect(await screen.findByText("No results")).toBeInTheDocument();
  });
});

describe("Graph Explorer — node detail", () => {
  it("shows node type, natural key, source and attributes for a selected node", async () => {
    mockApi();
    renderGraph();
    await screen.findByTestId("graph-legend");
    await search("node_a");
    await screen.findByTestId("graph-canvas-counts");

    const svg = screen.getByTestId("graph-canvas-svg");
    await waitFor(() =>
      expect(svg.querySelectorAll("g[cursor='pointer']").length).toBeGreaterThan(0),
    );
    fireEvent.click(svg.querySelectorAll("g[cursor='pointer']")[0]);

    const panel = await screen.findByTestId("graph-node-detail");
    expect(panel).toHaveTextContent("LinuxHost");
    expect(panel).toHaveTextContent("node_a");
  });
});

describe("Graph Explorer — scale honesty and active-only edges", () => {
  it("requests active edges only by default", async () => {
    const spy = mockApi();
    renderGraph();
    await screen.findByTestId("graph-legend");
    await search("node_a");
    await waitFor(() => {
      const call = spy.mock.calls.map(([u]) => String(u)).find((u) => u.includes(`/api/graph/kg/${HOST_ID}`));
      expect(call).toContain("active_only=true");
    });
  });

  it("says 'showing N of M' rather than silently truncating", async () => {
    mockApi({
      [`/api/graph/kg/${HOST_ID}`]: {
        ...HOST_NEIGHBOURHOOD,
        node_total: 5000,
        edge_total: 9000,
        truncated: true,
      },
    });
    renderGraph();
    await screen.findByTestId("graph-legend");
    await search("node_a");

    const overlay = await screen.findByTestId("graph-canvas-counts");
    await waitFor(() => expect(overlay).toHaveTextContent(/showing 2 of 5000 nodes/i));
    expect(overlay).toHaveTextContent(/9000/);
  });

  it("warns when the walk stopped at the defensive ceiling, so totals aren't read as exact", async () => {
    mockApi({
      [`/api/graph/kg/${HOST_ID}`]: {
        ...HOST_NEIGHBOURHOOD,
        node_total: 5000,
        truncated: true,
        walk_ceiling_hit: true,
      },
    });
    renderGraph();
    await screen.findByTestId("graph-legend");
    await search("node_a");
    expect(await screen.findByText(/at least/i)).toBeInTheDocument();
  });
});

describe("Graph Explorer — the legacy store is unreachable (P5)", () => {
  it("every graph request the page makes is a /kg/ request", async () => {
    const spy = mockApi();
    renderGraph();
    await screen.findByTestId("graph-legend");
    await search("node_a");
    await screen.findByTestId("graph-canvas-counts");

    const graphCalls = spy.mock.calls
      .map(([url]) => String(url))
      .filter((u) => u.startsWith("/api/graph/"));
    expect(graphCalls.length).toBeGreaterThan(0);
    for (const u of graphCalls) {
      expect(u.startsWith("/api/graph/kg/") || u.startsWith("/api/graph/entity-resolution/")).toBe(
        true,
      );
    }
  });
});

describe("Graph Explorer — error state", () => {
  it("surfaces a stats load failure instead of rendering an empty legend", async () => {
    vi.spyOn(api, "apiGet").mockRejectedValue(new Error("graph_nodes does not exist"));
    renderGraph();
    expect(await screen.findByText(/graph_nodes does not exist/)).toBeInTheDocument();
  });

  // The "does NOT blank the page when the store you are not looking at fails"
  // test that used to sit here is not ported. It existed because BOTH stores'
  // stats loaded on mount and an outage in the unselected one must not have
  // been fatal. With one store there is no unselected one, and the census
  // failing IS fatal for the Explorer (no legend, no edge filter) — which the
  // test above asserts directly.

  it("surfaces a neighbourhood load failure without blanking the whole page", async () => {
    vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
      if (url.includes("/api/graph/kg/stats")) return KG_STATS;
      if (url.includes("/api/graph/kg/search")) return { candidates: [kgNode()], total: 1, limit: 25 };
      if (url.includes(`/api/graph/kg/${HOST_ID}`)) throw new Error("node walk exploded");
      throw new Error(`unexpected apiGet: ${url}`);
    });
    renderGraph();
    await screen.findByTestId("graph-legend");
    await search("node_a");
    expect(await screen.findByText(/node walk exploded/)).toBeInTheDocument();
    // The legend (loaded fine) must still be on screen — one failed panel does
    // not blank the page.
    expect(screen.getByTestId("graph-legend")).toBeInTheDocument();
  });
});

import { useCallback, useEffect, useRef, useState } from "react";
import { apiGet, apiPost, AuthRequired, redirectToLogin } from "../api";
import { Skeleton } from "../components/Skeleton";
import { headerFor } from "../lib/headers";
import { Icon } from "../lib/icons";
import { useAutoRefresh } from "../hooks/useAutoRefresh";
import { PageShell, Panel, Button, Badge, Tabs, EmptyState } from "../components/ui";
import { hexA, YELLOW } from "../lib/tokens";
import { fmtDt } from "../lib/fmtDt";

/** Knowledge Graph page — SVG force-directed layout, MR-I renderer rewrite (KG-7).
 *
 * Legacy source:
 *  - Markup: dashboard/src/pages/graph.dc.html (isGraph block)
 *  - Derived values: src/infra_brain/dashboard/static/index.html:7227-7253
 *    (graphVals segment of renderVals())
 *  - Force layout algorithm: src/infra_brain/dashboard/static/index.html:4282-4360
 *    (`_renderGraph` — NOT support.js; the line numbers cited in the wave-6
 *    plan for support.js drifted, the implementation lives in index.html).
 *
 * MR-I / KG-7 (docs/audit/ROADMAP.md): "Replace O(n²) synchronous SVG/DOM force
 * layout and blind 100-node truncation before pursuing any '10k+ node'
 * visualization goal." The DR-6.1 React port (B3) had carried the legacy
 * algorithm over unchanged — a fixed 150-iteration O(n²) settle pass that ran
 * synchronously inside a single `useEffect` tick, blocking the main thread for
 * the whole layout, plus a hardcoded 100-node client truncation that didn't
 * match the backend's actual cap.
 *
 * What changed here, and why:
 *  1. RENDERER APPROACH — chose "time-slice the existing algorithm" over
 *     "adopt a physics library" (e.g. d3-force). Reasoning: d3-force would add
 *     a new supply-chain dependency to an app whose whole raison d'être (per
 *     DR-6.1) is *reducing* vendored/third-party surface area (the SRI-hash
 *     brittleness that justified retiring the DC shell), and would require
 *     verifying its behavior matches well enough not to visually regress the
 *     one already-shipped graph page. The actual defect reported (KG-7) is
 *     "synchronous, blocks the thread," not "wrong algorithm" — so this keeps
 *     the exact same force math (identical qualitative layout) and instead
 *     runs it across multiple animation frames, each budgeted to ~8ms of
 *     iteration work (`FRAME_BUDGET_MS`) before painting and yielding back to
 *     the browser via `requestAnimationFrame`. It also stops early once the
 *     centering-pull displacement drops below a small threshold instead of
 *     always running the full 150 iterations, so small/already-settled graphs
 *     finish in a couple of frames rather than paying the fixed cost every time.
 *  2. NODE/EDGE CAPS — the client-side display cap this section used to
 *     describe is gone. Capping happens SERVER-side now
 *     (`/api/graph/kg/{node_id}?max_nodes=`), which is the only place it can
 *     be done without shipping the whole result to the browser and then hiding
 *     most of it; see the "ONE STORE" note below.
 *  3. CONFIDENCE ENCODING — edges now vary stroke width and opacity by their
 *     `confidence` field (already returned by the backend, e.g.
 *     src/infra_brain/graph_api.py's relationship rows) and the edge label
 *     appends the confidence value, so a low-confidence inferred relationship
 *     (e.g. an `IS_SAME_AS` reconciliation guess) visually reads as thinner
 *     and fainter than a high-confidence one — previously confidence was
 *     fetched by the page (RelationshipRow.confidence, used in the sidebar
 *     relationship table) but never encoded on the canvas itself.
 *
 * WAVE 3.4 SELF-LOOP FILTER (preserved): the legacy `_renderGraph` builds its
 * node map by resource id, then filters edges with
 * `.filter(e=>e.s&&e.t&&e.s!==e.t)` (index.html:4296) — i.e. it drops any edge
 * whose from/to resolve to the SAME node object, which only happens when
 * from_resource_id === to_resource_id. This is a client-side belt-and-suspenders
 * copy of the server-side F-022 self-loop filter (src/infra_brain/db/relationships.py
 * emit_edge/emit_edges_batch, and the `<>` guard in graph_api.py's relationship
 * SQL). Reproduced below as the equivalent, more directly-expressed
 * `e.from_resource_id !== e.to_resource_id` check — kept exactly to avoid ever
 * drawing a self-referential loop on the canvas, even if a future data source
 * bypasses the backend filters.
 *
 * PHASE 3 DESIGN-SYSTEM REBUILD (see dashboard-app/DESIGN.md): this pass
 * converts only the surrounding chrome — PageHeader → PageShell, the
 * relationship-type chip bar, legend, and relationship/selected-node side
 * panels → Panel/DataTable/Button/EmptyState. The SVG force-directed canvas
 * itself (types, DOMAIN_COLORS, the iteration/render functions, and the
 * imperative renderGraphIncremental effect below) is a graphic visualization,
 * not tabular data — DESIGN.md §8 is explicit that a widget whose *shape*
 * carries the meaning stays a hand-rolled graphic, so none of that is touched.
 *
 * ── ONE STORE (graph-first P5) ──────────────────────────────────────────────
 * Everything above was built when `resource_relationships` was the only edge
 * table with data in it. It no longer is, and as of P5 it is no longer read at
 * all: the declarative graph engine (src/infra_brain/graph_engine.py)
 * materialises `graph_nodes` / `graph_edges` — the bitemporal store — from the
 * `etl.spec` contracts, and that is where RUNS_ON (service→host), BELONGS_TO
 * (iac file→project) and DEFINED_IN (project→file) live.
 *
 * The store TOGGLE this page carried through P4 is gone with the endpoints
 * behind it (/api/graph/relationships, /search, /{resource_id}), as is the
 * "legacy store — frozen, removal planned" notice: that notice existed to warn
 * an operator that the data they were looking at was on its way out, and the
 * honest end state of that warning is not a permanent banner, it is the view
 * not being there. A toggle whose second position 404s is worse than no
 * toggle.
 *
 * What survived the cut, and what deliberately did NOT change:
 *
 *  * NOT a new visualisation framework. The force layout, the frame budgeting,
 *    the confidence encoding, the self-loop filter and the whole SVG builder
 *    below are untouched. They were rewritten for KG-7 and there is nothing
 *    wrong with them.
 *  * The renderer keeps its STORE-NEUTRAL shape (`RenderNode`/`RenderEdge`)
 *    even though only one adapter (`kgToRender`) now feeds it. The neutral
 *    shape is what kept the layout code out of the migration entirely; it
 *    costs one small mapping function and it is the reason this file's hardest
 *    code needed no edits across a store swap.
 *  * SCALE. The page never slices a response client-side. `/api/graph/kg/
 *    {node_id}?max_nodes=` caps in the database and returns its own
 *    `node_total` / `edge_total` / `truncated` / `walk_ceiling_hit`, so the
 *    canvas states "showing N of M" for both nodes and edges, and says so when
 *    even M is only a floor. */

// ── graph_nodes / graph_edges store (/api/graph/kg/*) ───────────────────────

type KgNode = {
  id: string;
  /** graph_nodes.node_type — "LinuxHost", "HomelabService", "GitlabProject"… */
  type: string;
  name: string;
  natural_key: string;
  source: string;
  resource_id: string | null;
  attributes: Record<string, unknown>;
  first_seen: string | null;
  last_seen: string | null;
};

type KgEdge = {
  id: string;
  source_id: string;
  target_id: string;
  edge_type: string;
  method: string;
  confidence: number;
  authority: string;
  source: string;
  /** Non-null = retired. Only ever present when active_only=false was asked for. */
  valid_to: string | null;
};

type KgGraphOut = {
  root_id: string;
  nodes: KgNode[];
  edges: KgEdge[];
  node_total: number;
  edge_total: number;
  truncated: boolean;
  depth: number;
  active_only: boolean;
  walk_ceiling_hit: boolean;
};

type KgStatRow = { type: string; count: number };
type KgStats = {
  node_types: KgStatRow[];
  edge_types: KgStatRow[];
  total_nodes: number;
  total_edges: number;
};

/** The store-neutral shape the SVG renderer consumes. Nothing below this line
 *  knows which table the data came from — which is exactly why the P5 store
 *  swap needed no changes to the layout code at all. */
type RenderNode = { id: string; label: string; color: string };
type RenderEdge = { from: string; to: string; label: string; confidence: number };
type RenderGraph = { nodes: RenderNode[]; edges: RenderEdge[] };

type ReconciliationCandidate = {
  node_id: string;
  node_type: string;
  natural_key: string;
  name: string;
  source: string;
  score: number;
  reason: string;
};

type ReconciliationRow = {
  action_id: string;
  source_node: {
    node_id: string;
    node_type: string;
    natural_key: string;
    name: string;
    source: string;
  };
  candidate_matches: ReconciliationCandidate[];
  status: string;
  best_score: number;
  approved_by: string | null;
  created_at: string | null;
  retraction_history: {
    approved_by: string | null;
    retracted_by: string;
    retracted_at: string;
    reason: string | null;
  }[];
};

/** Requested server-side node cap. Enforced before the rows leave Postgres,
 *  and the response reports what it capped — the page never slices. */
const KG_MAX_NODES = 200;

/** Palette for node TYPES in the KG store.
 *
 * `graph_nodes.node_type` is a FREE STRING by contract (etl.spec.NodeSpec is
 * explicit that it is deliberately not constrained to the `GraphNodeType`
 * enum, so a new collector can declare a node type without a central edit).
 * A hardcoded map would therefore go stale by design — every new declaration
 * would render grey until someone remembered this file. So: curated colours
 * for the types that exist today, and a deterministic hash into the same
 * palette for anything else, which gives a NEW type a stable, distinct colour
 * immediately rather than a placeholder. */
const NODE_TYPE_COLORS: Record<string, string> = {
  LinuxHost: "#fb923c",
  HomelabService: "#4ade80",
  GitlabProject: "#38bdf8",
  IaCFile: "#94a3b8",
  VsphereVM: "#c084fc",
  VsphereHost: "#a78bfa",
  VsphereDatastore: "#818cf8",
  R7Asset: "#f87171",
  Cve: "#fb7185",
  AnsibleManagedHost: "#fbbf24",
  OctopusMachine: "#60a5fa",
};

const TYPE_FALLBACK_PALETTE = [
  "#2dd4bf", "#a3e635", "#facc15", "#22d3ee", "#e879f9",
  "#34d399", "#f472b6", "#84cc16", "#a8a29e", "#9ca3af",
];

function colorForNodeType(type: string): string {
  const curated = NODE_TYPE_COLORS[type];
  if (curated) return curated;
  // djb2-ish: stable across reloads and across users, so the same type is
  // always the same colour in a screenshot or a shared link.
  let hash = 5381;
  for (let i = 0; i < type.length; i++) hash = ((hash << 5) + hash + type.charCodeAt(i)) | 0;
  return TYPE_FALLBACK_PALETTE[Math.abs(hash) % TYPE_FALLBACK_PALETTE.length];
}

/** KG store → renderer shape. Node colour is by node TYPE — the meaningful
 *  axis in this store, where there is no `domain` column at all. */
function kgToRender(data: KgGraphOut, edgeTypeFilter: Set<string>): RenderGraph {
  const edges = data.edges.filter(
    (e) => edgeTypeFilter.size === 0 || edgeTypeFilter.has(e.edge_type),
  );
  // With a filter on, a node left with no surviving edge is noise — except the
  // root, which is the thing the user actually asked about and must never
  // vanish from its own neighbourhood view.
  const connected = new Set<string>([data.root_id]);
  for (const e of edges) {
    connected.add(e.source_id);
    connected.add(e.target_id);
  }
  const nodes = data.nodes.filter((n) => edgeTypeFilter.size === 0 || connected.has(n.id));
  return {
    nodes: nodes.map((n) => ({
      id: n.id,
      label: n.name || n.natural_key || "",
      color: colorForNodeType(n.type),
    })),
    edges: edges.map((e) => ({
      from: e.source_id,
      to: e.target_id,
      label: e.edge_type || "",
      confidence: e.confidence,
    })),
  };
}

/** Max iteration-work budget per animation frame, in milliseconds. Chosen so
 * a single frame of layout work stays comfortably under one 16.7ms (60fps)
 * frame, leaving headroom for the browser's own paint/composite work — this
 * is what makes the layout "async/incremental" instead of one blocking call. */
const FRAME_BUDGET_MS = 8;

const MAX_ITERATIONS = 150; // same total ceiling the legacy algorithm used
const CONVERGED_THRESHOLD = 0.4; // px of max per-node displacement; below this we stop early

type PNode = RenderNode & { x: number; y: number };
type PEdge = RenderEdge & { s: PNode; t: PNode };

/** One pass of the same Fruchterman-Reingold-style force math the legacy
 * `_renderGraph` (index.html:4282-4360) used, unchanged — see the file header
 * comment for why the algorithm itself wasn't replaced. Returns the largest
 * single-node displacement this iteration produced, used as a convergence
 * signal by the caller. */
function runOneIteration(nodes: PNode[], edges: PEdge[], k: number, W: number, H: number): number {
  for (let i = 0; i < nodes.length; i++) {
    for (let j = i + 1; j < nodes.length; j++) {
      const dx = nodes[i].x - nodes[j].x;
      const dy = nodes[i].y - nodes[j].y;
      const d = Math.max(Math.sqrt(dx * dx + dy * dy), 1);
      const f = (k * k) / d;
      nodes[i].x += (dx / d) * f * 0.1;
      nodes[i].y += (dy / d) * f * 0.1;
      nodes[j].x -= (dx / d) * f * 0.1;
      nodes[j].y -= (dy / d) * f * 0.1;
    }
  }
  for (const e of edges) {
    const dx = e.t.x - e.s.x;
    const dy = e.t.y - e.s.y;
    const d = Math.max(Math.sqrt(dx * dx + dy * dy), 1);
    const f = (d * d) / k;
    e.s.x += (dx / d) * f * 0.05;
    e.s.y += (dy / d) * f * 0.05;
    e.t.x -= (dx / d) * f * 0.05;
    e.t.y -= (dy / d) * f * 0.05;
  }
  let maxDisp = 0;
  for (const n of nodes) {
    const beforeX = n.x;
    const beforeY = n.y;
    n.x += (W / 2 - n.x) * 0.01;
    n.y += (H / 2 - n.y) * 0.01;
    n.x = Math.max(30, Math.min(W - 30, n.x));
    n.y = Math.max(30, Math.min(H - 30, n.y));
    maxDisp = Math.max(maxDisp, Math.hypot(n.x - beforeX, n.y - beforeY));
  }
  return maxDisp;
}

/** Confidence -> visual encoding for an edge (MR-I / KG-7). Low-confidence
 * inferred relationships (e.g. an IS_SAME_AS reconciliation guess) render
 * thin and faint; high-confidence ones render thick and solid. Confidence is
 * assumed to be a 0..1 float, same range the backend already emits. */
function edgeConfidenceStyle(confidence: number) {
  const c = Number.isFinite(confidence) ? Math.max(0, Math.min(1, confidence)) : 0.5;
  return { strokeWidth: 1 + c * 3, opacity: 0.3 + c * 0.6 };
}

type RenderHandle = { cancel: () => void };

/** Async/incremental replacement for the legacy `_renderGraph`'s single
 * blocking 150-iteration pass (KG-7 / see file header comment for full
 * rationale). Builds the SVG elements once, then advances the force
 * simulation across multiple `requestAnimationFrame` callbacks — each frame
 * runs iterations until `FRAME_BUDGET_MS` elapses, repaints element
 * positions from the current node coordinates, and yields back to the
 * browser — instead of computing all 150 iterations synchronously before
 * anything is ever painted. Stops early if the layout converges
 * (CONVERGED_THRESHOLD) before MAX_ITERATIONS. Returns a handle to cancel the
 * in-flight animation loop (called on unmount / when `data` changes again,
 * so a slow layout for a previous query can't keep painting over a new one). */
function renderGraphIncremental(
  svgEl: SVGSVGElement,
  data: RenderGraph,
  onNodeClick: (nodeId: string) => void,
): RenderHandle {
  const W = svgEl.clientWidth || 900;
  const H = svgEl.clientHeight || 520;

  const nm = new Map<string, PNode>(
    data.nodes.map((n) => [
      n.id,
      { ...n, x: W / 2 + (Math.random() - 0.5) * 200, y: H / 2 + (Math.random() - 0.5) * 200 },
    ]),
  );
  const nodes = [...nm.values()];

  // Wave 3.4 self-loop filter, preserved (see file-level comment). Expressed
  // against the neutral shape now, but it is the same check on the same edge.
  const edges: PEdge[] = data.edges
    .filter((e) => e.from !== e.to)
    .map((e) => ({ ...e, s: nm.get(e.from), t: nm.get(e.to) }))
    .filter((e): e is PEdge => !!e.s && !!e.t);

  const k = Math.sqrt((W * H) / Math.max(nodes.length, 1));

  const NS = "http://www.w3.org/2000/svg";
  const mk = (tag: string, at: Record<string, string | number>) => {
    const el = document.createElementNS(NS, tag);
    Object.entries(at).forEach(([key, v]) => el.setAttribute(key, String(v)));
    return el;
  };

  svgEl.innerHTML = "";
  const defs = svgEl.appendChild(mk("defs", {}));
  defs.innerHTML =
    '<marker id="ib-arr" viewBox="0 -5 10 10" refX="20" refY="0" markerWidth="6" markerHeight="6" orient="auto"><path d="M0,-5L10,0L0,5" fill="var(--ib-border)"/></marker>';

  const eg = svgEl.appendChild(mk("g", {}));
  const edgeEls = edges.map((e) => {
    const { strokeWidth, opacity } = edgeConfidenceStyle(e.confidence);
    const line = eg.appendChild(
      mk("line", {
        stroke: "var(--ib-faint)",
        "stroke-width": strokeWidth,
        opacity,
        "marker-end": "url(#ib-arr)",
      }),
    ) as SVGLineElement;
    const lbl = eg.appendChild(
      mk("text", {
        fill: "var(--ib-muted)",
        "font-size": 9,
        "text-anchor": "middle",
        "dominant-baseline": "middle",
      }),
    ) as SVGTextElement;
    const confText = Number.isFinite(e.confidence) ? ` ${e.confidence.toFixed(2)}` : "";
    // KG-6: 12 chars collided HAS_HARDWARE_VENDOR/HAS_HARDWARE_MODEL into
    // the same "HAS_HARDWARE" label. 20 chars disambiguates every current
    // RelationshipType (longest is RUNS_TENTACLE_VERSION at 22 chars, but
    // the first 20 chars of any two DISTINCT current members are already
    // unique — verified against the full enum in
    // src/infra_brain/db/relationships.py at plan-authoring time). A
    // native <title> element is also added so a future new RelationshipType
    // member that reintroduces a collision at 20 chars is still fully
    // readable on hover, without needing another line-length bump. The same
    // budget comfortably covers the KG store's edge types (RUNS_ON,
    // BELONGS_TO, DEFINED_IN, HOSTED_ON, MOUNTS_DATASTORE, AFFECTED_BY_CVE,
    // SAME_AS, NOT_SAME_AS — longest is 16).
    const fullLabel = e.label || "";
    lbl.textContent = `${fullLabel.slice(0, 20)}${confText}`;
    const lblTitle = document.createElementNS(NS, "title");
    lblTitle.textContent = `${fullLabel}${confText}`;
    lbl.appendChild(lblTitle);
    return { e, line, lbl };
  });

  const ng = svgEl.appendChild(mk("g", {}));
  const nodeEls = nodes.map((n) => {
    const g = ng.appendChild(mk("g", { cursor: "pointer" })) as SVGGElement;
    g.appendChild(
      mk("circle", { r: 14, fill: n.color, opacity: 0.85, "stroke-width": 1.5, stroke: "rgba(255,255,255,.15)" }),
    );
    const tx = mk("text", {
      y: 24,
      fill: "var(--ib-text)",
      "font-size": 9,
      "text-anchor": "middle",
      "font-family": "DM Mono,monospace",
    });
    tx.textContent = n.label.slice(0, 18);
    g.appendChild(tx);
    g.addEventListener("click", () => onNodeClick(n.id));
    return { n, g };
  });

  function paint() {
    for (const { n, g } of nodeEls) g.setAttribute("transform", `translate(${n.x},${n.y})`);
    for (const { e, line, lbl } of edgeEls) {
      line.setAttribute("x1", String(e.s.x));
      line.setAttribute("y1", String(e.s.y));
      line.setAttribute("x2", String(e.t.x));
      line.setAttribute("y2", String(e.t.y));
      lbl.setAttribute("x", String((e.s.x + e.t.x) / 2));
      lbl.setAttribute("y", String((e.s.y + e.t.y) / 2));
    }
  }

  let iteration = 0;
  let cancelled = false;
  let rafId = 0;

  function step() {
    if (cancelled) return;
    const frameStart = performance.now();
    let lastDisp = 0;
    while (iteration < MAX_ITERATIONS && performance.now() - frameStart < FRAME_BUDGET_MS) {
      lastDisp = runOneIteration(nodes, edges, k, W, H);
      iteration++;
    }
    paint();
    if (iteration < MAX_ITERATIONS && lastDisp > CONVERGED_THRESHOLD) {
      rafId = requestAnimationFrame(step);
    }
  }
  rafId = requestAnimationFrame(step);

  return {
    cancel: () => {
      cancelled = true;
      cancelAnimationFrame(rafId);
    },
  };
}

/** One review-queue question: a source node with N ranked candidate matches.
 *  Panel-per-row, not DataTable — each row is a heterogeneous source-node
 *  header plus an ordered candidate list, the same "Panel over DataTable"
 *  call this file already makes for Decisions.tsx-shaped data (DESIGN.md §8),
 *  not a homogeneous per-field grain. */
function ReviewQueueRow({
  row,
  onResolved,
}: {
  row: ReconciliationRow;
  onResolved: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [confirmingReject, setConfirmingReject] = useState(false);
  const [confirmingRetract, setConfirmingRetract] = useState(false);

  const onAuth = (e: unknown) => {
    if (e instanceof AuthRequired) redirectToLogin();
    else setMsg(e instanceof Error ? e.message : String(e));
  };

  async function handleConfirm(targetNodeId: string, name: string) {
    setBusy(true);
    setMsg(`Confirming match with ${name}…`);
    try {
      await apiPost(
        `/api/graph/entity-resolution/${encodeURIComponent(row.action_id)}/confirm`,
        { target_node_id: targetNodeId }
      );
      onResolved();
      setMsg(`Confirmed as the same entity as ${name}.`);
    } catch (e) {
      onAuth(e);
    } finally {
      setBusy(false);
    }
  }

  async function handleReject() {
    if (!confirmingReject) {
      setConfirmingReject(true);
      return;
    }
    setBusy(true);
    setMsg("Rejecting…");
    try {
      await apiPost(`/api/dashboard/actions/${encodeURIComponent(row.action_id)}/reject`);
      onResolved();
      setMsg("Rejected — no candidate matches.");
    } catch (e) {
      onAuth(e);
    } finally {
      setBusy(false);
      setConfirmingReject(false);
    }
  }

  async function handleRetract() {
    if (!confirmingRetract) {
      setConfirmingRetract(true);
      return;
    }
    setBusy(true);
    setMsg("Retracting…");
    try {
      await apiPost(`/api/graph/entity-resolution/${encodeURIComponent(row.action_id)}/retract`, {
        reason: "Retracted from the dashboard review queue.",
      });
      onResolved();
      setMsg("Retracted — the edge was undone and this question is open again.");
    } catch (e) {
      onAuth(e);
    } finally {
      setBusy(false);
      setConfirmingRetract(false);
    }
  }

  const isPending = row.status === "pending";
  const isApproved = row.status === "approved";

  return (
    <Panel
      mnemonic={row.source_node.node_type}
      description={`${row.source_node.source} · ${row.source_node.natural_key}`}
      rail={isPending ? "warn" : row.status === "approved" ? "ok" : "info"}
    >
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
        <strong style={{ fontSize: 14 }}>{row.source_node.name}</strong>
        <Badge tone={isPending ? "warn" : row.status === "approved" ? "ok" : "neutral"}>{row.status}</Badge>
      </div>
      <div style={{ fontSize: 12, color: "var(--ib-faint)", marginBottom: 10 }}>
        Is this the same entity as one of these candidates from other sources?
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        {row.candidate_matches.map((c) => (
          <div
            key={c.node_id}
            style={{
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center",
              gap: 8,
              padding: "8px 10px",
              background: "var(--ib-raised)",
              borderRadius: "var(--ib-radius-sm)",
            }}
          >
            <div style={{ minWidth: 0 }}>
              <div style={{ fontSize: 13, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {c.name} <span style={{ color: "var(--ib-faint)" }}>({c.source})</span>
              </div>
              <div style={{ fontSize: 11, color: "var(--ib-faint)" }}>
                score {c.score.toFixed(3)} — {c.reason}
              </div>
            </div>
            {isPending && (
              <Button size="sm" variant="primary" disabled={busy} onClick={() => handleConfirm(c.node_id, c.name)}>
                Confirm match
              </Button>
            )}
          </div>
        ))}
      </div>
      {isPending && (
        <div style={{ marginTop: 10 }}>
          <Button size="sm" variant={confirmingReject ? "danger" : "ghost"} disabled={busy} onClick={handleReject}>
            {confirmingReject ? "Click again to confirm reject" : "Reject — none of these match"}
          </Button>
        </div>
      )}
      {isApproved && (
        <div style={{ marginTop: 10 }}>
          <Button size="sm" variant={confirmingRetract ? "danger" : "ghost"} disabled={busy} onClick={handleRetract}>
            {confirmingRetract ? "Click again to confirm retract" : "Retract — this was a mistake"}
          </Button>
        </div>
      )}
      {msg && <div style={{ marginTop: 8, fontSize: 12, color: "var(--ib-faint)" }}>{msg}</div>}
      {row.approved_by && (
        <div style={{ marginTop: 8, fontSize: 11, color: "var(--ib-faint)" }}>
          Resolved by {row.approved_by}
        </div>
      )}
      {isPending && row.retraction_history.length > 0 && (
        <div style={{ marginTop: 8, fontSize: 11, color: "var(--ib-faint)" }}>
          ⚠ Previously confirmed by{" "}
          {row.retraction_history[row.retraction_history.length - 1].approved_by ?? "someone"}, then
          retracted by {row.retraction_history[row.retraction_history.length - 1].retracted_by} — this
          is a reopened mistake, not an untouched question.
        </div>
      )}
    </Panel>
  );
}

/** Cross-source entity-resolution review queue (TRK-226) — the human side of
 *  graph_phase3.resolve_entities' fuzzy match band: a question the automatic
 *  pass deliberately did not resolve on its own gets confirmed/rejected here. */
function ReviewQueueTab() {
  const [items, setItems] = useState<ReconciliationRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (opts?: { silent?: boolean }) => {
    if (!opts?.silent) setLoading(true);
    try {
      const d = await apiGet<{ items: ReconciliationRow[]; total: number }>(
        "/api/graph/entity-resolution/queue"
      );
      setItems(d.items || []);
      setError(null);
    } catch (e) {
      if (e instanceof AuthRequired) redirectToLogin();
      else setError(e instanceof Error ? e.message : String(e));
    } finally {
      if (!opts?.silent) setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const handleResolved = () => {
    // Re-fetch the real row rather than hand-patching one field: confirm,
    // reject, and retract each change more than `status` (approved_by,
    // retraction_history, ...) and a stale local patch previously left
    // "Resolved by X" rendering on a row that retract had just reopened to
    // pending. Silent -- no loading-skeleton flash over an interaction the
    // operator is mid-way through reading the result of.
    load({ silent: true });
  };

  if (loading) {
    return <Skeleton height={90} count={3} />;
  }
  if (error) {
    return <EmptyState kind="error" title="Review queue failed to load" hint={error} />;
  }

  if (items.length === 0) {
    return (
      <EmptyState
        kind="none-yet"
        title="Nothing to review"
        hint="No ambiguous cross-source identity questions are pending. graph_maintenance's Phase 3 entity-resolution pass queues a row here only when a fuzzy match falls in its review band, or two candidates are ambiguous."
      />
    );
  }

  // A single map over `items` (already pending-first, then most recent, per
  // get_reconciliation_state's own sort) rather than two separate filtered
  // maps -- rendering pending/resolved as two adjacent `{a.map()}{b.map()}`
  // blocks loses each ReviewQueueRow's local component state (its post-action
  // confirmation message) the instant a row moves from one block to the
  // other, even with the same `key`, since the two blocks are the render
  // engine's separate reconciliation domains despite being sibling children.
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      {items.map((row) => (
        <ReviewQueueRow key={row.action_id} row={row} onResolved={handleResolved} />
      ))}
    </div>
  );
}

export function Graph() {
  const [activeTab, setActiveTab] = useState<"explorer" | "review">("explorer");
  const svgRef = useRef<SVGSVGElement>(null);
  const [query, setQuery] = useState("");
  const [queryLoading, setQueryLoading] = useState(false);
  const [warning, setWarning] = useState("");
  /** Non-fatal: a failed search/traversal. Rendered inline so the rest of the
   *  page (legend, store summary, review queue) survives one bad request. */
  const [queryError, setQueryError] = useState<string | null>(null);
  /** Fatal: without the type census there is no legend and no edge filter, so
   *  the Explorer has nothing to render. (Through P4 this was one of TWO
   *  per-store error slots, because both stores' stats loaded on mount and a
   *  failure in the one you were NOT looking at had to stay non-fatal. With one
   *  store there is only one answer to "did the census load".) */
  const [statsErrorKg, setStatsErrorKg] = useState<string | null>(null);

  const [kgStats, setKgStats] = useState<KgStats | null>(null);
  const [kgStatsLoading, setKgStatsLoading] = useState(true);
  const [kgCandidates, setKgCandidates] = useState<KgNode[]>([]);
  const [kgData, setKgData] = useState<KgGraphOut | null>(null);
  const [selectedKg, setSelectedKg] = useState<KgNode | null>(null);
  /** Empty = show every edge type. Non-empty = show only these. */
  const [edgeTypeFilter, setEdgeTypeFilter] = useState<Set<string>>(new Set());
  /** valid_to IS NULL by default — the current state of the estate, not its
   *  full bitemporal history. Opt into history explicitly. */
  const [activeOnly, setActiveOnly] = useState(true);

  const asMessage = (e: unknown) => (e instanceof Error ? e.message : String(e));
  const onQueryError = (e: unknown) => {
    if (e instanceof AuthRequired) redirectToLogin();
    else setQueryError(asMessage(e));
  };

  const loadKgStats = useCallback(async () => {
    setKgStatsLoading(true);
    try {
      const d = await apiGet<KgStats>(`/api/graph/kg/stats?active_only=${activeOnly}`);
      setKgStats(d);
      setStatsErrorKg(null);
    } catch (e) {
      if (e instanceof AuthRequired) redirectToLogin();
      else setStatsErrorKg(e instanceof Error ? e.message : String(e));
    } finally {
      setKgStatsLoading(false);
    }
  }, [activeOnly]);

  // The type census loads on mount: it is a single cheap GROUP BY and it feeds
  // both the legend and the edge-type filter, so deferring it buys a blank
  // panel, not performance. The loader swallows into its own error state, so no
  // .catch() is needed here.
  useEffect(() => {
    void loadKgStats();
  }, [loadKgStats]);

  useAutoRefresh(loadKgStats);

  // Imperative render pass — mirrors the legacy setTimeout(...,50) after
  // setState in _searchGraph/_openGraphNode, replaced here with a plain
  // effect keyed on `data` (runs once the SVG has mounted with real data).
  // MR-I: the render is now an in-flight animation (renderGraphIncremental
  // returns a cancel handle) rather than a single synchronous call, so a
  // layout still settling for a previous query is explicitly cancelled
  // before starting a new one — otherwise a slow old animation frame could
  // paint stale positions over the new graph after the user searches again.
  //
  // `activeTab` is also a dependency (not just `kgData`): the
  // `<svg ref={svgRef}>` lives inside the `activeTab === "explorer"`
  // conditional subtree below, so switching to Review Queue unmounts it and
  // switching back mounts a brand new, empty SVG node. Without `activeTab`
  // here, this effect stays keyed only on `kgData` — which hasn't changed — so
  // it would never re-run and the freshly-mounted SVG would stay permanently
  // blank even though `kgData` (and the node/edge count overlay that reads it)
  // is still populated.
  //
  // `edgeTypeFilter` is a dependency for the same reason: it changes what
  // should be on the canvas without the data object changing identity.
  useEffect(() => {
    if (activeTab !== "explorer" || !svgRef.current) return;
    const graph = kgData && kgToRender(kgData, edgeTypeFilter);
    if (!graph) return;
    const handle = renderGraphIncremental(svgRef.current, graph, (nodeId) => {
      setSelectedKg(kgData?.nodes.find((n) => n.id === nodeId) ?? null);
    });
    return () => handle.cancel();
  }, [kgData, activeTab, edgeTypeFilter]);

  // ── Loaders ───────────────────────────────────────────────────────────────

  function resetQueryState() {
    setWarning("");
    setQueryError(null);
    setKgCandidates([]);
    setKgData(null);
    setSelectedKg(null);
  }

  async function openKgNode(id: string) {
    setQueryLoading(true);
    resetQueryState();
    try {
      const d = await apiGet<KgGraphOut>(
        `/api/graph/kg/${encodeURIComponent(id)}?depth=2&max_nodes=${KG_MAX_NODES}` +
          `&active_only=${activeOnly}`,
      );
      setKgData(d);
    } catch (e) {
      onQueryError(e);
    } finally {
      setQueryLoading(false);
    }
  }

  async function search() {
    const q = query.trim();
    if (!q) return;
    setQueryLoading(true);
    resetQueryState();
    try {
      const d = await apiGet<{ candidates: KgNode[]; total: number; limit: number }>(
        `/api/graph/kg/search?q=${encodeURIComponent(q)}&limit=25`,
      );
      const found = d.candidates || [];
      if (found.length === 1) {
        // Exactly one match: open it. Making the operator click a
        // one-item list to confirm the only possible answer is friction, not
        // disambiguation — the ambiguity warning is what the >1 branch is for.
        setQueryLoading(false);
        await openKgNode(found[0].id);
        return;
      }
      if (d.total > found.length) {
        setWarning(
          `${d.total} nodes match — showing the first ${found.length}. Narrow the search to see the rest.`,
        );
      }
      setKgCandidates(found);
    } catch (e) {
      onQueryError(e);
    } finally {
      setQueryLoading(false);
    }
  }

  function toggleEdgeType(type: string) {
    setEdgeTypeFilter((prev) => {
      const next = new Set(prev);
      if (next.has(type)) next.delete(type);
      else next.add(type);
      return next;
    });
  }

  const h = headerFor("graph");

  if (statsErrorKg) {
    return (
      <div>
        <PageShell icon={<Icon name={h.icon} />} title={h.title} description={h.desc} />
        <EmptyState
          kind="error"
          title="Graph failed to load"
          hint={statsErrorKg}
          action={{ label: "Retry", onClick: () => void loadKgStats() }}
        />
      </div>
    );
  }

  // The graph currently on the canvas. Computed once here so the count overlay
  // and the empty-state logic below cannot disagree with what was actually
  // drawn — the filter can remove nodes, and an overlay reading the raw
  // response would then be lying.
  const rendered: RenderGraph | null = kgData ? kgToRender(kgData, edgeTypeFilter) : null;
  const nodeCount = rendered?.nodes.length ?? 0;
  const edgeCount = rendered?.edges.length ?? 0;

  // What the STORE said it found, before this page's filter — the backend
  // walked further than it returned and reports both numbers.
  const nodeTotal = kgData?.node_total ?? 0;
  const edgeTotal = kgData?.edge_total ?? 0;
  const isTruncated = nodeCount < nodeTotal || edgeCount < edgeTotal;

  const isEmpty =
    !!(kgData && kgData.nodes.length === 0 && !queryLoading) ||
    (!kgData && kgCandidates.length === 0 && !queryLoading && !!query.trim() && !queryError);
  // TRK-238 item 2: before any search has run the canvas rendered as a large
  // bordered box with nothing in it — no indication that this is expected
  // ("nothing searched yet") rather than a load failure. Distinct from
  // `isEmpty` (a search that legitimately returned nothing, messaged above the
  // canvas) — this covers the "never queried" state instead.
  const noQueryYet = !kgData && !queryLoading && kgCandidates.length === 0;

  // Legend: scope to the node types actually on the canvas once there is one;
  // otherwise the whole store's type census, which doubles as "what kinds of
  // thing does this graph know about at all".
  const kgNodeTypeLegend: KgStatRow[] = kgData
    ? Array.from(
        kgData.nodes.reduce(
          (acc, n) => acc.set(n.type, (acc.get(n.type) ?? 0) + 1),
          new Map<string, number>(),
        ),
      )
        .map(([type, count]) => ({ type, count }))
        .sort((a, b) => b.count - a.count || a.type.localeCompare(b.type))
    : (kgStats?.node_types ?? []);
  const kgEdgeTypeLegend: KgStatRow[] = kgStats?.edge_types ?? [];

  return (
    <main>
      <PageShell icon={<Icon name={h.icon} />} title={h.title} description={h.desc} />

      <div style={{ marginBottom: 16 }}>
        <Tabs
          label="Graph sections"
          active={activeTab}
          onChange={(k) => setActiveTab(k as "explorer" | "review")}
          tabs={[
            { key: "explorer", label: "Explorer" },
            { key: "review", label: "Review Queue" },
          ]}
        />
      </div>

      {activeTab === "review" && <ReviewQueueTab />}

      {activeTab === "explorer" && (
      <>
      <div style={{ marginBottom: 16 }}>
        <Panel mnemonic="STORE" description="graph_nodes / graph_edges" rail="info">
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center" }}>
            <Button
              variant={activeOnly ? "primary" : "ghost"}
              size="sm"
              onClick={() => setActiveOnly((v) => !v)}
              title="graph_edges is bitemporal — a retired edge keeps its row with valid_to stamped. Active-only is the current state of the estate."
            >
              {activeOnly ? "Active edges only" : "Including retired edges"}
            </Button>
          </div>
          <div
            data-testid="graph-store-summary"
            style={{ fontSize: 11, color: "var(--ib-faint)", marginTop: 10 }}
          >
            {kgStatsLoading && !kgStats ? (
              <div data-testid="graph-kg-stats-skeleton" style={{ width: 240 }}>
                <Skeleton height={14} count={1} />
              </div>
            ) : kgStats && kgStats.total_nodes > 0 ? (
              <>
                Store holds <strong>{kgStats.total_nodes.toLocaleString()}</strong> nodes across{" "}
                {kgStats.node_types.length} type{kgStats.node_types.length === 1 ? "" : "s"} and{" "}
                <strong>{kgStats.total_edges.toLocaleString()}</strong>{" "}
                {activeOnly ? "active" : "total"} edges across {kgStats.edge_types.length} type
                {kgStats.edge_types.length === 1 ? "" : "s"}. Search for a host, service, project or
                file to explore it.
              </>
            ) : (
              <>
                The knowledge graph is empty — run the graph_maintenance sweep to materialise nodes
                and edges from the collectors' declarations.
              </>
            )}
          </div>
        </Panel>
      </div>

      {kgEdgeTypeLegend.length > 0 && (
        <div style={{ marginBottom: 16 }}>
          <Panel
            mnemonic="EDGETYPES"
            description={
              edgeTypeFilter.size === 0
                ? "Click a type to show only its edges on the canvas"
                : `Filtering to ${edgeTypeFilter.size} of ${kgEdgeTypeLegend.length} edge types`
            }
            rail="info"
          >
            <div style={{ display: "flex", flexWrap: "wrap", gap: 8, alignItems: "center" }}>
              {kgEdgeTypeLegend.map((et) => (
                <Button
                  key={et.type}
                  variant={edgeTypeFilter.has(et.type) ? "primary" : "ghost"}
                  size="sm"
                  onClick={() => toggleEdgeType(et.type)}
                >
                  {et.type}: {et.count}
                </Button>
              ))}
              {edgeTypeFilter.size > 0 && (
                <Button variant="ghost" size="sm" onClick={() => setEdgeTypeFilter(new Set())}>
                  Clear filter
                </Button>
              )}
            </div>
          </Panel>
        </div>
      )}

      <div style={{ display: "grid", gridTemplateColumns: "1fr 340px", gap: 16, alignItems: "start" }}>
        <div>
          <div style={{ display: "flex", gap: 8, marginBottom: 14 }}>
            <input
              data-testid="graph-search-input"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") search();
              }}
              placeholder="Search host, IP, or resource name..."
              style={{
                flex: 1,
                background: "var(--ib-raised)",
                border: "1px solid var(--ib-border)",
                borderRadius: "var(--ib-radius-sm)",
                padding: "9px 14px",
                color: "var(--ib-text)",
                fontSize: 13,
                outline: "none",
              }}
            />
            <Button variant="primary" onClick={search}>
              Search
            </Button>
          </div>

          {warning && (
            <div
              style={{
                fontSize: 12,
                color: YELLOW,
                background: hexA(YELLOW, 0.08),
                border: `1px solid ${hexA(YELLOW, 0.22)}`,
                borderRadius: "var(--ib-radius)",
                padding: "8px 14px",
                marginBottom: 12,
              }}
            >
              {warning}
            </div>
          )}

          {queryError && (
            <div style={{ marginBottom: 12 }}>
              <EmptyState
                kind="error"
                title="That query failed"
                hint={queryError}
                action={{ label: "Try again", onClick: () => search() }}
              />
            </div>
          )}

          {kgCandidates.length > 0 && (
            <div style={{ marginBottom: 12 }}>
              <Panel mnemonic="MATCHES" description="Multiple matches — pick one" rail="info">
                <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
                  {kgCandidates.map((c) => (
                    <Button key={c.id} variant="ghost" size="sm" onClick={() => openKgNode(c.id)}>
                      {c.name} ({c.type})
                    </Button>
                  ))}
                </div>
              </Panel>
            </div>
          )}

          {queryLoading && (
            <div style={{ padding: 24 }}>
              <Skeleton height={14} count={3} />
            </div>
          )}

          {isEmpty && !queryError && (
            <EmptyState
              kind="filter-zero"
              title="No results"
              hint="Nothing in graph_nodes matches that name or natural key. Node names come from the collectors — try a host name, a service name, a GitLab project, or an IaC file path."
            />
          )}

          <div
            style={{
              background: "var(--ib-bg)",
              border: "1px solid var(--ib-border)",
              borderRadius: "var(--ib-radius)",
              overflow: "hidden",
              position: "relative",
            }}
          >
            <svg ref={svgRef} data-testid="graph-canvas-svg" style={{ width: "100%", height: 520, display: "block" }} />
            {noQueryYet && (
              <div
                style={{
                  position: "absolute",
                  inset: 0,
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                }}
              >
                <EmptyState
                  kind="none-yet"
                  title="No graph data to show yet"
                  hint="Search above for a host to see its services, or a project to see its files."
                />
              </div>
            )}
            {nodeCount > 0 && (
              <div
                data-testid="graph-canvas-counts"
                style={{
                  position: "absolute",
                  top: 10,
                  right: 12,
                  fontSize: 10,
                  color: isTruncated ? YELLOW : "var(--ib-faint)",
                  fontFamily: "var(--ib-mono, 'DM Mono', monospace)",
                  textAlign: "right",
                }}
              >
                {/* Truncation is stated, never silent: the canvas says what
                    fraction of the found subgraph it is drawing, for BOTH
                    nodes and edges (the old warning covered nodes only). */}
                <div>
                  {isTruncated
                    ? `showing ${nodeCount} of ${nodeTotal} nodes · ${edgeCount} of ${edgeTotal} edges`
                    : `${nodeCount} nodes · ${edgeCount} edges`}
                </div>
                {kgData?.walk_ceiling_hit && (
                  <div style={{ marginTop: 2 }}>
                    walk stopped at the safety ceiling — at least this many exist
                  </div>
                )}
                {edgeTypeFilter.size > 0 && (
                  <div style={{ marginTop: 2, opacity: 0.8 }}>
                    filtered to {Array.from(edgeTypeFilter).join(", ")}
                  </div>
                )}
                <div style={{ marginTop: 2, opacity: 0.8 }}>edge thickness/opacity = confidence</div>
              </div>
            )}
          </div>
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
          <Panel mnemonic="LEGEND" rail="info">
            <div data-testid="graph-legend">
              <div style={{ fontSize: 10, fontWeight: 700, color: "var(--ib-muted)", marginBottom: 6 }}>
                {/* Colour is by node TYPE: graph_nodes has no `domain` column
                    at all — it has a per-source `node_type`, which is the
                    thing worth colouring. */}
                Node types
              </div>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 12 }}>
                {kgNodeTypeLegend.map((nt) => (
                  <span
                    key={nt.type}
                    style={{ display: "flex", alignItems: "center", gap: 4, fontSize: 10, color: "var(--ib-muted)" }}
                  >
                    <span
                      style={{
                        width: 8,
                        height: 8,
                        borderRadius: "50%",
                        background: colorForNodeType(nt.type),
                        display: "inline-block",
                      }}
                    />
                    <span>{nt.type}</span>
                    <span style={{ color: "var(--ib-faint)" }}>{nt.count}</span>
                  </span>
                ))}
                {kgNodeTypeLegend.length === 0 && !kgStatsLoading && (
                  <span style={{ fontSize: 10, color: "var(--ib-faint)" }}>No node types yet.</span>
                )}
              </div>
              <div style={{ fontSize: 10, fontWeight: 700, color: "var(--ib-muted)", marginBottom: 6 }}>Edge types</div>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 6, maxHeight: 120, overflowY: "auto" }}>
                {kgEdgeTypeLegend.map((et) => (
                  <Button
                    key={et.type}
                    variant={edgeTypeFilter.has(et.type) ? "primary" : "ghost"}
                    size="sm"
                    onClick={() => toggleEdgeType(et.type)}
                  >
                    {et.type} {et.count}
                  </Button>
                ))}
                {kgEdgeTypeLegend.length === 0 && !kgStatsLoading && (
                  <span style={{ fontSize: 10, color: "var(--ib-faint)" }}>No edge types yet.</span>
                )}
              </div>
            </div>
          </Panel>

          {selectedKg && (
            <Panel mnemonic="NODE" description="Selected Node" rail="info">
              <div data-testid="graph-node-detail" style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4 }}>
                  <span
                    style={{
                      width: 10,
                      height: 10,
                      borderRadius: "50%",
                      background: colorForNodeType(selectedKg.type),
                      display: "inline-block",
                      flexShrink: 0,
                    }}
                  />
                  <span style={{ fontSize: 14, fontWeight: 700, color: "var(--ib-text)", wordBreak: "break-all" }}>
                    {selectedKg.name}
                  </span>
                  <Badge tone="neutral">{selectedKg.type}</Badge>
                </div>
                {[
                  // natural_key is the node's IDENTITY in this store — the
                  // thing that makes two rows the same entity — so it is shown
                  // rather than hidden behind the display name it often
                  // differs from ("node_a/litellm" vs "litellm").
                  ["Natural key", selectedKg.natural_key],
                  ["Source", selectedKg.source],
                  ["First seen", fmtDt(selectedKg.first_seen) || "—"],
                  ["Last seen", fmtDt(selectedKg.last_seen) || "—"],
                ].map(([label, value]) => (
                  <div key={label} style={{ display: "flex", justifyContent: "space-between", gap: 8 }}>
                    <span style={{ fontSize: 11, color: "var(--ib-muted)", flexShrink: 0 }}>{label}</span>
                    <span
                      style={{ fontSize: 11, color: "var(--ib-text)", fontWeight: 600, wordBreak: "break-all", textAlign: "right" }}
                    >
                      {value}
                    </span>
                  </div>
                ))}
                {Object.keys(selectedKg.attributes || {}).length > 0 && (
                  <>
                    <div style={{ fontSize: 10, fontWeight: 700, color: "var(--ib-muted)", marginTop: 8 }}>
                      Attributes
                    </div>
                    {Object.entries(selectedKg.attributes).map(([k, v]) => (
                      <div key={k} style={{ display: "flex", justifyContent: "space-between", gap: 8 }}>
                        <span style={{ fontSize: 11, color: "var(--ib-muted)", flexShrink: 0 }}>{k}</span>
                        <span
                          style={{ fontSize: 11, color: "var(--ib-text)", wordBreak: "break-all", textAlign: "right" }}
                        >
                          {typeof v === "object" ? JSON.stringify(v) : String(v)}
                        </span>
                      </div>
                    ))}
                  </>
                )}
                {/* `resource_id` is still shown when present — it is the
                    bridge into the `resources` id space that the detail pages
                    and the MCP tools use. What is gone is the "Open in legacy
                    store" button next to it: there is no other store to open
                    it in. */}
                {selectedKg.resource_id && (
                  <div style={{ display: "flex", justifyContent: "space-between", gap: 8 }}>
                    <span style={{ fontSize: 11, color: "var(--ib-muted)", flexShrink: 0 }}>
                      Resource id
                    </span>
                    <span
                      style={{
                        fontSize: 11,
                        color: "var(--ib-text)",
                        wordBreak: "break-all",
                        textAlign: "right",
                        fontFamily: "var(--ib-mono, 'DM Mono', monospace)",
                      }}
                    >
                      {selectedKg.resource_id}
                    </span>
                  </div>
                )}
                <div style={{ display: "flex", gap: 6, marginTop: 10, flexWrap: "wrap" }}>
                  {selectedKg.id !== kgData?.root_id && (
                    <Button size="sm" variant="primary" onClick={() => openKgNode(selectedKg.id)}>
                      Explore from here
                    </Button>
                  )}
                </div>
              </div>
            </Panel>
          )}
        </div>
      </div>
      </>
      )}
    </main>
  );
}

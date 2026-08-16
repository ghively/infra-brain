import { useEffect, useMemo, useState, useCallback } from "react";
import { Link, useNavigate } from "react-router-dom";
import { apiGet, rows, AuthRequired, redirectToLogin } from "../api";
import { Skeleton } from "../components/Skeleton";
import {
  Badge,
  Button,
  DataTable,
  DomainCell,
  DriftTapeInstrument,
  EmptyState,
  PageShell,
  Panel,
  PanelFootItem,
  RelationshipMiniGraph,
  StatTile,
  SummaryBlock,
  TrendBarChart,
} from "../components/ui";
import type {
  ColumnDef,
  DriftTapeZone,
  HealthSegment,
  PanelRail,
  RelationshipEdge,
  RelationshipSatellite,
  SummaryStat,
  TrendBarDatum,
} from "../components/ui";
import { headerFor } from "../lib/headers";
import { BLUE, RED, hexA } from "../lib/tokens";
import { useAutoRefresh } from "../hooks/useAutoRefresh";

/** Home — the dashboard's flagship page, rebuilt on the Phase 1 design system
 * (see `dashboard-app/DESIGN.md` and the redesign plan at
 * `~/.claude/plans/tidy-inventing-duckling.md`, Phase 2).
 *
 * This is a *visual + structural* rebuild on top of the page's existing real
 * logic, not a rewrite: every data source, derivation and accuracy fix from the
 * previous 656-line hand-styled version is preserved. Where it moved, it moved
 * to a primitive that owns the contract:
 *
 *  | Old surface                        | New surface                             |
 *  |------------------------------------|-----------------------------------------|
 *  | 5 hand-styled health cards         | `SummaryBlock` hero + 4 stats (§8)      |
 *  | 5-tile Intelligence Loop strip     | `LOOP` Panel of `StatTile`s             |
 *  | absolutely-positioned 7-day chart  | `TREND` Panel + `TrendBarChart`         |
 *  | (new) volatile daily drift level   | `DRIFT` Panel + `DriftTapeInstrument`   |
 *  | Live Activity div feed             | `ACTIVITY` Panel + `DataTable`          |
 *  | Domain Health card grid            | `DOMAIN` Panel + sortable `DataTable`   |
 *  | (new) adjacency / blast radius     | `GRAPH` Panel + `RelationshipMiniGraph` |
 *
 * Accuracy fixes carried forward verbatim (do not regress these):
 *  - A1: run status is compared against BOTH `"failure"` (the API's display
 *    mapping in `api/_helpers.py::_RUN_STATUS`) and `"failed"` (the raw DB
 *    value), tolerant like `Topbar.tsx`.
 *  - A2: open-drift uses `counts.open_drift` — the server-side COUNT that
 *    bypasses the list endpoints' `.limit(500)` cap — never a client-side
 *    filter over the capped `/drift_events` page.
 *  - A3: `/scan_points` and `/notifications` are paginated envelopes; read
 *    `total`, not `items.length`, which only reflects the capped page.
 *
 * Two deliberate judgment calls, recorded so they aren't mistaken for losses:
 *  1. The 7-day chart's *stacked-by-domain* colouring is gone. `TrendBarChart`
 *     is a 2-series (total + highlighted subset) instrument, and DESIGN.md
 *     allows exactly four semantic hues — a per-domain colour ramp would have
 *     needed a fifth, sixth, ... hue with no semantic meaning. Per-domain
 *     drift survives as an exact sortable number in the DOMAIN table rather
 *     than as a colour the user has to decode against a legend. (F-10 update:
 *     the chart's data source moved to the `/drift_events/trend` aggregate,
 *     which has no `status` dimension, so the second series is real-zero — see
 *     the `trendData` comment below — rather than the total-vs-still-open
 *     split this originally shipped with.)
 *  2. Every health-card click-through survived, but as the stat's own dim
 *     caption (`SummaryStat.sub`) rather than a whole-card link: /drift,
 *     /vulns, /eol, /collruns from the summary, /invrec from the LOOP panel's
 *     Learn tile, /hosts from the hero.
 */

const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** Relationship types that `graph_phase3.blast_radius` treats as risk-carrying
 *  — these render as the mini-graph's dashed red "flagged" edges. Matching on
 *  the edge type keeps the flag derived from real data rather than invented. */
const FLAGGED_EDGE_RE = /cve|vuln|eol|affect|expos|risk/i;

/** How many satellites the mini-graph shows around its hub. Beyond ~9 the
 *  circular layout's labels start colliding at this panel size. */
const MAX_SATELLITES = 9;

type Counts = {
  open_drift: number;
  open_cves: number;
  eol_overdue: number;
  eol_total: number;
  invrec_proposed: number;
  invrec_total: number;
  total_resources: number;
  applied_instincts: number;
};

export type DriftRow = { id: string; domain: string; status: string; detected_at?: string };
type ActivityRow = {
  ts: string;
  agent: string;
  tool: string;
  verdict: string;
  latency_ms: number;
};
type ResourceRow = { domain: string; status: string };
type RunRow = { status: string; started_at: string };

/** A row from `GET /api/graph/kg/edges` (`graph_api.py::KgEdgeRow`).
 *
 *  Graph-first P5: this panel used to read `/api/graph/relationships`, which
 *  resolved against `resource_relationships` and keyed its endpoints in the
 *  `resources.id` space. That store is being dropped, so the panel moved to
 *  the graph_edges census — a different id space (`graph_nodes.id`), hence the
 *  field rename rather than a quiet swap of the URL underneath the same names.
 *  `source_name`/`target_name` are the human labels the FK rule (DESIGN.md §5)
 *  requires; they default to `""` server-side when unresolvable. */
type RelRow = {
  source_id: string;
  target_id: string;
  edge_type: string;
  confidence?: number;
  source_name?: string;
  target_name?: string;
};

type DriftDay = { label: string; key: string; total: number };

/** A point from `GET /api/dashboard/drift_events/trend` (`governance_drift.py::
 *  get_drift_trend`, `DriftTrendPoint`) — server-side aggregate: one row per
 *  (date, domain) with a count. No `status` dimension exists at this
 *  granularity (see F-10 note on `buildDriftDays` below). */
export type DriftTrendPoint = { date: string; count: number; domain: string };

/** Maps a resource domain string to the nav.ts route that best represents it,
 *  so DOMAIN table rows navigate somewhere sensible on click. Falls back to
 *  /hosts (the canonical inventory view) for anything unrecognized.
 *  Preserved unchanged from the pre-rebuild page. */
function domainPath(domain: string): string {
  const d = domain.toLowerCase();
  if (/vsphere/.test(d)) return "/vsphere";
  if (/(vuln|cve)/.test(d)) return "/vulns";
  if (/eol/.test(d)) return "/eol";
  if (/(compliance|compl)/.test(d)) return "/compl";
  if (/(security|audit)/.test(d)) return "/security";
  if (/software/.test(d)) return "/software";
  if (/(linux|windows|net|cloud|discovery|host|inventory)/.test(d)) return "/hosts";
  return "/hosts";
}

/**
 * Bucket the `/drift_events/trend` server aggregate (F-10: a per-(date,domain)
 * COUNT, not a full unbounded page of raw drift rows) into the last 7 days,
 * today back to 6 days ago.
 *
 * F-6 fix: the previous version built its 7 bucket keys from LOCAL calendar
 * days (`new Date()` local getters) but indexed events by the UTC date
 * (`detected_at.slice(0, 10)`, since `detected_at` is always serialized as an
 * ISO `Z` string) — two different zones on the two sides of the same lookup.
 * For a UTC-5/UTC-4 operator, any event detected between ~19:00 and midnight
 * local already has a UTC date of "tomorrow", so its key never matched a
 * local-day bucket and it was silently dropped from the tape, the trend
 * chart, and `weekTotal`. This version buckets BOTH sides in one zone —
 * UTC — consistently: the bucket keys are built from UTC getters, matching
 * the UTC dates the trend endpoint's `func.date()` aggregate and every
 * `detected_at` string already use.
 *
 * There is no `status` dimension at this (date, domain) granularity, so
 * unlike the pre-F-10 version there is no `open` subset here — see the
 * `TrendBarChart` `subset` comment where `driftDays` is consumed below (same
 * documented real-omission Drift.tsx's own TREND widget already carries for
 * this identical endpoint).
 */
export function buildDriftDays(points: DriftTrendPoint[]): DriftDay[] {
  const days: DriftDay[] = [];
  const now = new Date();
  for (let i = 6; i >= 0; i--) {
    const d = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() - i));
    const key = `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, "0")}-${String(d.getUTCDate()).padStart(2, "0")}`;
    days.push({ label: `${MON[d.getUTCMonth()]} ${d.getUTCDate()}`, key, total: 0 });
  }
  const idx = new Map(days.map((d) => [d.key, d]));
  points.forEach((p) => {
    const bucket = idx.get(String(p.date ?? "").slice(0, 10));
    if (!bucket) return;
    bucket.total += p.count;
  });
  return days;
}

/**
 * Threshold bands for the drift tape, anchored on the week's own mean rather
 * than on invented absolute numbers: at-or-below a typical day is healthy,
 * up to 2× typical is a warning, above that is critical. This is a *display*
 * band for "today vs. this week", not a policy threshold.
 */
function driftZones(days: DriftDay[]): { zones: DriftTapeZone[]; max: number } {
  const totals = days.map((d) => d.total);
  const peak = Math.max(0, ...totals);
  if (peak === 0) {
    // Nothing detected all week — a single green band over a nominal scale, so
    // the instrument still reads as "healthy" rather than rendering empty.
    return { zones: [{ color: "green", from: 0, to: 5 }], max: 5 };
  }
  const mean = totals.reduce((a, b) => a + b, 0) / Math.max(1, totals.length);
  const max = Math.max(peak, Math.ceil(mean * 3));
  const warn = Math.max(1, Math.round(mean));
  const crit = Math.max(warn + 1, Math.round(mean * 2));
  return {
    zones: [
      { color: "green", from: 0, to: warn },
      { color: "yellow", from: warn, to: crit },
      { color: "red", from: crit, to: max },
    ],
    max,
  };
}

/**
 * Fold `/api/graph/kg/edges` rows into a hub-and-satellites view: the
 * highest-degree node becomes the hub, its neighbours become satellites, and
 * risk-carrying edge types (per `FLAGGED_EDGE_RE`) render flagged. This is a
 * real read of the knowledge graph — see the module docstring in
 * `graph_api.py` — not a placeholder.
 */
function buildBlastRadius(rels: RelRow[]): {
  hub: { id: string; label: string };
  satellites: RelationshipSatellite[];
  edges: RelationshipEdge[];
} | null {
  if (rels.length === 0) return null;

  const degree = new Map<string, number>();
  const nameById = new Map<string, string>();
  const note = (id: string, name?: string) => {
    if (!id) return;
    degree.set(id, (degree.get(id) ?? 0) + 1);
    if (name && !nameById.get(id)) nameById.set(id, name);
  };
  rels.forEach((r) => {
    note(r.source_id, r.source_name);
    note(r.target_id, r.target_name);
  });

  let hubId = "";
  let hubDegree = -1;
  degree.forEach((n, id) => {
    if (n > hubDegree) {
      hubDegree = n;
      hubId = id;
    }
  });
  if (!hubId) return null;

  // FK rule (DESIGN.md §5): an id is never presented as if it were a name. In
  // an SVG node label a full uuid cannot fit, so an unresolved node is marked
  // with a leading `?` and truncated — explicitly "an id we could not resolve",
  // not a hostname.
  const label = (id: string) => nameById.get(id) || `?${id.slice(0, 8)}`;

  const satellites: RelationshipSatellite[] = [];
  const edges: RelationshipEdge[] = [];
  const seen = new Set<string>([hubId]);

  for (const r of rels) {
    if (satellites.length >= MAX_SATELLITES) break;
    const other =
      r.source_id === hubId ? r.target_id : r.target_id === hubId ? r.source_id : null;
    if (!other || seen.has(other)) continue;
    seen.add(other);
    const flagged = FLAGGED_EDGE_RE.test(r.edge_type);
    satellites.push({ id: other, label: label(other), flagged });
    edges.push({
      fromId: hubId,
      toId: other,
      style: flagged ? "flagged" : "normal",
      edgeType: r.edge_type,
    });
  }

  if (satellites.length === 0) return null;
  return { hub: { id: hubId, label: label(hubId) }, satellites, edges };
}

export function Home() {
  const navigate = useNavigate();
  const [counts, setCounts] = useState<Counts | null>(null);
  // Bounded, page-scoped raw events — feeds ONLY the DOMAIN table's
  // per-domain open-drift counts (F-10: no longer also re-walked client-side
  // to build the 7-day chart; see `driftTrend` below for that).
  const [drift, setDrift] = useState<DriftRow[] | null>(null);
  // Server-aggregated per-day counts from `/drift_events/trend` (F-10) — feeds
  // the DRIFT tape + TREND chart. Far smaller than the ~200KB unbounded
  // `/drift_events` page the pre-F-10 version fetched just for this.
  const [driftTrend, setDriftTrend] = useState<DriftTrendPoint[] | null>(null);
  const [activity, setActivity] = useState<ActivityRow[] | null>(null);
  const [resources, setResources] = useState<ResourceRow[] | null>(null);
  const [runs, setRuns] = useState<RunRow[] | null>(null);
  // Scan points + notification tickets feed the Intelligence Loop's Trigger/Notify
  // stage metrics (legacy index.html:5988/5994 used SCANS.length / NOTIFS.length).
  // A1/A2/A3 accuracy fix: both endpoints are paginated ({items,total,...}) and
  // capped (scan_points default limit=100, notifications here requests
  // limit=500) — rows(d).length only reflects the page, not the true total. Use
  // the envelope's `total` field, same pattern as Agents.tsx/CollRuns.tsx/etc.
  const [scanCount, setScanCount] = useState<number | null>(null);
  const [notifCount, setNotifCount] = useState<number | null>(null);
  // Knowledge-graph adjacency for the GRAPH panel. Deliberately NOT part of the
  // page's loading gate and never surfaces an error: `/api/graph/kg/edges` is a
  // separate router from `/api/dashboard/*`, so a deployment without the graph
  // populated must degrade to the panel's own EmptyState, not blank the whole
  // dashboard.
  const [rels, setRels] = useState<RelRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [lastRefreshed, setLastRefreshed] = useState<Date | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  const driftDays = useMemo(() => buildDriftDays(driftTrend ?? []), [driftTrend]);
  const blast = useMemo(() => buildBlastRadius(rels), [rels]);

  const load = useCallback(async () => {
    // F-3: only the very first load (before anything has ever rendered) may
    // block the whole page on a failure. `loading` (computed below from
    // these same state setters, which only ever fire on success and never
    // reset a field to null) is already false forever once every required
    // field has been populated at least once — so gating the full-page
    // EmptyState on `loading` (see the render section) rather than on `error`
    // alone means a later refresh's failure simply leaves stale-but-real data
    // on screen, same as `usePageData`. `onAuth` itself stays unconditional;
    // it is harmless to keep recording the latest error string even once it
    // no longer drives a blocking render — the global fetch-error banner
    // (`api.ts`'s `fetchErrors` store, rendered by `GlobalErrorBanner`/
    // `EndpointErrorBanner` in App.tsx) is what actually carries the signal
    // for a post-first-load failure now.
    const onAuth = (e: unknown) => {
      if (e instanceof AuthRequired) redirectToLogin();
      else setError(String(e));
    };
    setRefreshing(true);
    await Promise.allSettled([
      apiGet<Counts>("/api/dashboard/counts").then(setCounts).catch(onAuth),
      // F-10: bounded to a page (matches the `resources` fetch's own
      // `?limit=200` precedent below) and `suppress_telemetry=true` — this
      // list is what the DOMAIN table's per-domain open-drift counts read,
      // and the Drift page's own list view (Drift.tsx) also defaults its
      // `suppress` toggle to `true`; without this param Home's domain-health
      // badges counted routine R7 telemetry noise the Drift page hides by
      // default, so the two pages disagreed about "how much is open" for the
      // same domain. Chosen direction: suppress here too, to match Drift's
      // list default. (The day-bucketed trend fetch just below can NOT be
      // suppressed the same way — `/drift_events/trend` has no
      // `suppress_telemetry` param server-side — so it stays consistent
      // instead with Drift.tsx's own TREND widget, which reads this same
      // endpoint the same unsuppressed way.)
      apiGet<unknown>("/api/dashboard/drift_events?limit=200&suppress_telemetry=true")
        .then((d) => setDrift(rows<DriftRow>(d)))
        .catch(onAuth),
      // F-10: the 7-day DRIFT tape + TREND chart now read the server-side
      // per-day aggregate instead of re-deriving day buckets from an
      // unbounded (server default limit=500, ~200KB) full page of raw drift
      // rows fetched on every 5-minute auto-refresh and every manual Refresh.
      apiGet<unknown>("/api/dashboard/drift_events/trend?days=7")
        .then((d) => {
          const points = (d as { points?: unknown }).points;
          setDriftTrend(Array.isArray(points) ? (points as DriftTrendPoint[]) : []);
        })
        .catch(onAuth),
      // F-10: bounded to exactly the 6 rows `activityFeed` renders below,
      // instead of the server default limit=500 (~166KB) fetched just to
      // slice off the first 6.
      apiGet<unknown>("/api/dashboard/activity?limit=6")
        .then((d) => setActivity(rows<ActivityRow>(d)))
        .catch(onAuth),
      // limit=200 mirrors the legacy default page size (_loadResources); this
      // page only needs domain/status for the domain table, not full detail.
      apiGet<unknown>("/api/dashboard/resources?limit=200")
        .then((d) => setResources(rows<ResourceRow>(d)))
        .catch(onAuth),
      apiGet<unknown>("/api/dashboard/collection_runs")
        .then((d) => setRuns(rows<RunRow>(d)))
        .catch(onAuth),
      // Scan points (Trigger stage) and notification tickets (Notify stage) — the
      // two Intelligence-Loop metrics not derivable from the core fetches above.
      // A3: use the envelope's true `total`, not the length of the (capped)
      // `items` page — rows() discards `total`, so read the raw response here.
      apiGet<unknown>("/api/dashboard/scan_points")
        .then((d) => setScanCount((d as { total?: number }).total ?? rows<unknown>(d).length))
        .catch(onAuth),
      apiGet<unknown>("/api/dashboard/notifications?limit=500")
        .then((d) => setNotifCount((d as { total?: number }).total ?? rows<unknown>(d).length))
        .catch(onAuth),
      // Optional: see the `rels` state comment. Auth still redirects; anything
      // else leaves the GRAPH panel empty rather than failing the page.
      apiGet<unknown>("/api/graph/kg/edges?limit=200")
        .then((d) => setRels(rows<RelRow>(d)))
        .catch((e: unknown) => {
          if (e instanceof AuthRequired) redirectToLogin();
          else setRels([]);
        }),
    ]);
    setLastRefreshed(new Date());
    setRefreshing(false);
  }, []);

  useEffect(() => {
    void load();
  }, [load]);
  useAutoRefresh(load);

  const h = headerFor("home");

  // F-3: `loading` is true only before the very first successful load of
  // every required field — since each setter above only ever fires on
  // success, once every field has been populated once this is false forever,
  // regardless of any later fetch failure (`usePageData`'s exact contract).
  // The blocking full-page EmptyState is therefore reachable ONLY while
  // nothing has ever loaded; a failure on a later refresh (auto or manual)
  // leaves the already-populated dashboard exactly as it was.
  const loading =
    counts === null ||
    drift === null ||
    driftTrend === null ||
    activity === null ||
    resources === null ||
    runs === null;

  if (loading) {
    if (error)
      return (
        <main>
          <EmptyState
            kind="error"
            title="Home failed to load"
            hint={error}
            action={{ label: "Retry", onClick: () => { setError(null); void load(); } }}
          />
        </main>
      );
    return <Skeleton count={4} height={90} />;
  }

  /* ---------------------------------------------------------------- *
   * Derivations (unchanged semantics from the pre-rebuild page)
   * ---------------------------------------------------------------- */

  // A2: counts.open_drift is the server-side COUNT (see fleet.py's /counts
  // endpoint comment: "bypass the .limit(500) caps ... never affected by the
  // .limit(N) caps on the corresponding list endpoints"). The drift-events
  // fetch above has no limit param, so it returns the default capped page —
  // filtering that client-side undercounts once open drift exceeds the cap.
  const openDrift = counts.open_drift;
  const succRun = runs
    .filter((r) => r.status === "success")
    .reduce((a: RunRow | null, b) => (!a || b.started_at > a.started_at ? b : a), null);
  const daysSince = succRun
    ? Math.max(0, Math.floor((Date.now() - new Date(succRun.started_at).getTime()) / 86400000))
    : null;
  const daysSinceStr = daysSince === null ? "—" : daysSince === 0 ? "Today" : `${daysSince}d`;

  // A1: the API maps raw DB statuses (failed/retry_exhausted) to the display
  // value "failure" (see api/_helpers.py::_RUN_STATUS) — "failed" never
  // appears in the response. Check both, tolerant like Topbar.tsx's dot-color
  // logic, so this is correct regardless of which exact string the backend
  // returns.
  const failedRuns = runs.filter((r) => r.status === "failure" || r.status === "failed").length;

  const domCount = new Map<string, number>();
  resources.forEach((r) => domCount.set(r.domain, (domCount.get(r.domain) ?? 0) + 1));
  const collectorCount = domCount.size;

  // Unlike A2 above, there is no server-side per-domain aggregate to draw from
  // here: `resources` is capped at `?limit=200` and `drift` has no `limit=`
  // param at all (server default `limit=500`, most-recent-first — see the load()
  // comment). Once `total_resources` or fleet-wide open drift exceeds those
  // caps, a domain's count/eol/openDrift below — and the health badge/rail
  // derived from them — can under-report for domains whose rows fall outside
  // the fetched page. Same limitation ResourcesTab.tsx's domain chips document
  // and label "(page)" rather than presenting as an authoritative total; do the
  // same here (column headers, chips, description, badge tooltip below).
  const domainRows = Array.from(domCount.keys())
    .sort()
    .map((d) => {
      const rs = resources.filter((r) => r.domain === d);
      const eol = rs.filter((r) => r.status === "eol").length;
      const openDriftForDomain = drift.filter((x) => x.domain === d && x.status === "open").length;
      // Same precedence the old card grid's dot color used: EOL outranks
      // drift, drift outranks healthy.
      const health: "ok" | "warn" | "err" = eol > 0 ? "warn" : openDriftForDomain > 0 ? "err" : "ok";
      return {
        domain: d,
        count: rs.length,
        eol,
        openDrift: openDriftForDomain,
        health,
        statusText: eol > 0 ? `${eol} EOL` : openDriftForDomain > 0 ? `${openDriftForDomain} open drift` : "healthy",
      };
    });

  const driftDomains = domainRows.filter((d) => d.health === "err").length;
  const eolDomains = domainRows.filter((d) => d.health === "warn").length;
  const okDomains = domainRows.length - driftDomains - eolDomains;

  // The rail tone matches the worst contained finding (DESIGN.md §6).
  const domainRail: PanelRail =
    domainRows.length === 0 ? "info" : driftDomains > 0 ? "err" : eolDomains > 0 ? "warn" : "ok";

  const health: HealthSegment[] =
    domainRows.length === 0
      ? []
      : [
          { color: "green", fraction: okDomains / domainRows.length, label: `${okDomains} healthy domains` },
          { color: "yellow", fraction: eolDomains / domainRows.length, label: `${eolDomains} domains with EOL assets` },
          { color: "red", fraction: driftDomains / domainRows.length, label: `${driftDomains} domains with open drift` },
        ];

  const { zones: tapeZones, max: tapeMax } = driftZones(driftDays);
  const today = driftDays[driftDays.length - 1];
  // F-10: `driftDays` now comes from the trend endpoint's (date, domain,
  // count) aggregate, which has no `status` dimension — there is no real
  // per-day "still open" figure to highlight any more, so `subset` stays
  // real-zero rather than fabricating a split. Same documented gap
  // Drift.tsx's own TrendBarChart already carries for this identical
  // endpoint (see DriftWidgets there).
  const trendData: TrendBarDatum[] = driftDays.map((d) => ({
    label: d.label,
    total: d.total,
    subset: 0,
  }));
  const weekTotal = driftDays.reduce((a, d) => a + d.total, 0);

  const activityFeed = activity.slice(0, 6);

  const stats: SummaryStat[] = [
    {
      label: "Open Drift",
      value: openDrift,
      color: openDrift > 0 ? "red" : "green",
      sub: <Link to="/drift">awaiting triage →</Link>,
    },
    {
      label: "Open CVEs",
      value: counts.open_cves,
      color: counts.open_cves > 0 ? "red" : "green",
      sub: <Link to="/vulns">unresolved fleet-wide →</Link>,
    },
    {
      label: "EOL Assets",
      value: counts.eol_total,
      color: counts.eol_overdue > 0 ? "yellow" : "green",
      sub: (
        <Link to="/eol">
          {counts.eol_overdue > 0 ? `${counts.eol_overdue} overdue →` : "none overdue →"}
        </Link>
      ),
    },
    {
      label: "Last Scan",
      value: daysSinceStr,
      color: failedRuns > 0 ? "red" : "green",
      sub: (
        <Link to="/collruns">
          {failedRuns > 0
            ? `${failedRuns} failed collection run${failedRuns === 1 ? "" : "s"} →`
            : "all runs healthy →"}
        </Link>
      ),
    },
  ];

  const domainColumns: ColumnDef<(typeof domainRows)[number]>[] = [
    {
      key: "domain",
      header: "domain",
      typeGlyph: "enum",
      width: 190,
      render: (r) => <DomainCell name={r.domain} />,
    },
    { key: "count", header: "resources (page)", typeGlyph: "num" },
    { key: "openDrift", header: "open drift (page)", typeGlyph: "num", render: (r) => r.openDrift },
    { key: "eol", header: "eol (page)", typeGlyph: "num", render: (r) => r.eol },
    {
      key: "health",
      header: "health",
      typeGlyph: "enum",
      sortable: false,
      render: (r) => (
        <Badge
          tone={r.health}
          title={`${r.domain}: ${r.statusText} (based on a page-scoped sample — see resources/drift caps in the DOMAIN panel description)`}
        >
          {r.statusText}
        </Badge>
      ),
    },
  ];

  const activityColumns: ColumnDef<ActivityRow>[] = [
    { key: "agent", header: "agent", typeGlyph: "text" },
    { key: "tool", header: "tool", typeGlyph: "text" },
    {
      key: "verdict",
      header: "verdict",
      typeGlyph: "enum",
      render: (a) => {
        const tone = a.verdict === "allow" ? "ok" : a.verdict === "deny" ? "err" : "warn";
        return <Badge tone={tone}>{a.verdict || "unknown"}</Badge>;
      },
    },
    { key: "ts", header: "at", typeGlyph: "text", render: (a) => a.ts.slice(11, 19) },
    { key: "latency_ms", header: "ms", typeGlyph: "num", render: (a) => a.latency_ms },
  ];

  const lastRefreshedStr = lastRefreshed
    ? `Refreshed ${lastRefreshed.toTimeString().slice(0, 8)}`
    : "—";

  return (
    <main>
      <PageShell
        title={h.title}
        description={h.desc}
        icon="📊"
        actions={
          <>
            <span className="ib-mono" style={{ fontSize: 11, color: "var(--faint)" }}>
              {lastRefreshedStr}
            </span>
            <Button size="sm" onClick={() => void load()} loading={refreshing}>
              Refresh
            </Button>
          </>
        }
      />

      {/* Hero — the single owner of every top-line fleet number. The segmented
       *  health bar under the hero value is the domain composition the old card
       *  grid encoded as per-tile dots. */}
      <SummaryBlock
        hero={{
          label: "Fleet Resources",
          value: counts.total_resources,
          sub: (
            <Link to="/hosts">
              {collectorCount} domain{collectorCount === 1 ? "" : "s"} reporting → browse inventory
            </Link>
          ),
        }}
        stats={stats}
        health={health}
      />

      <div style={{ display: "grid", gridTemplateColumns: "1.55fr 1fr", gap: 16, marginTop: 16 }}>
        <Panel
          mnemonic="DOMAIN"
          description="resources ⨝ drift_events · per-domain counts are page-scoped"
          rail={domainRail}
          headerRight={`${domainRows.length} domains`}
          footer={
            <>
              <PanelFootItem>{okDomains} healthy</PanelFootItem>
              <PanelFootItem tone="warn">{eolDomains} with EOL assets</PanelFootItem>
              <PanelFootItem tone="err">{driftDomains} with open drift</PanelFootItem>
            </>
          }
        >
          {domainRows.length === 0 ? (
            <EmptyState
              kind="none-yet"
              title="No resources collected yet"
              hint="Domain health appears once a collection run completes."
            />
          ) : (
            <DataTable
              caption="Resource coverage and health by collection domain (per-domain counts are page-scoped)"
              columns={domainColumns}
              rows={domainRows}
              rowKey={(r) => r.domain}
              toolbarName="domain_health"
              chips={[`${counts.total_resources} resources`, "grouped by domain (page)"]}
              sortable
              initialSortKey="count"
              initialSortDir="desc"
              defaultDensity="compact"
              minWidth={520}
              onRowClick={(r) => navigate(domainPath(r.domain))}
              statusBar={{ shown: domainRows.length, total: domainRows.length }}
            />
          )}
        </Panel>

        <div style={{ display: "grid", gap: 16, alignContent: "start" }}>
          {/* Volatile single metric → cockpit tape (DESIGN.md §8). */}
          <Panel
            mnemonic="DRIFT"
            description="events detected per day"
            rail={openDrift > 0 ? "err" : "ok"}
            live
          >
            <div style={{ display: "flex", gap: 14, alignItems: "flex-start" }}>
              <DriftTapeInstrument
                label="Drift events today"
                value={today?.total ?? 0}
                min={0}
                max={tapeMax}
                zones={tapeZones}
                history={driftDays.map((d) => d.total)}
              />
              <div style={{ minWidth: 0 }}>
                <StatTile
                  label="Today"
                  value={today?.total ?? 0}
                  color={(today?.total ?? 0) > 0 ? "yellow" : "green"}
                  sub="detected in the last 24h"
                />
                <div style={{ height: 10 }} />
                <StatTile
                  label="This week"
                  value={weekTotal}
                  // F-10: the trend aggregate this now reads has no per-day
                  // `status`, so "still open" can no longer be computed for
                  // the week specifically — the real fleet-wide open count
                  // (`openDrift`, the same server aggregate the hero stat
                  // above uses) is shown instead of fabricating a per-week
                  // figure from data that no longer exists client-side.
                  sub={`${openDrift} open fleet-wide`}
                  color={openDrift > 0 ? "red" : "green"}
                />
              </div>
            </div>
          </Panel>

          {/* Composition over time → stacked bars. */}
          <Panel mnemonic="TREND" description="7 days · detected per day" rail="info">
            {weekTotal === 0 ? (
              <EmptyState
                kind="clean"
                title="No drift events in the last 7 days"
                hint="A quiet week is the expected steady state — not a sign collection stopped."
              />
            ) : (
              <TrendBarChart
                data={trendData}
                totalColor={hexA(BLUE, 0.38)}
                subsetColor={RED}
                // F-10: subset is real-zero (no status at trend-endpoint
                // granularity — see `trendData` above), labeled honestly
                // rather than implying a highlighted "still open" split.
                legend={{ totalLabel: "Drift events", subsetLabel: "no secondary breakdown available" }}
              />
            )}
          </Panel>
        </div>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16, marginTop: 16 }}>
        {/* Relationship / blast-radius view over the real knowledge graph. */}
        <Panel
          mnemonic="GRAPH"
          description="graph_relationships ⨝ resources"
          rail={blast?.satellites.some((s) => s.flagged) ? "err" : blast ? "info" : "info"}
          headerRight={blast ? `${blast.satellites.length} adjacent` : undefined}
        >
          {blast ? (
            <>
              <RelationshipMiniGraph
                hub={blast.hub}
                satellites={blast.satellites}
                edges={blast.edges}
              />
              <div style={{ fontSize: 11, color: "var(--muted)", marginTop: 6 }}>
                Highest-degree resource in the graph and its immediate blast radius. Dashed red
                edges are risk-carrying relationship types.
              </div>
            </>
          ) : (
            <EmptyState
              kind="none-yet"
              title="No graph relationships available"
              hint="This view reads GET /api/graph/kg/edges. It populates once the graph_maintenance sweep has materialised edges from the collectors' declarations."
              action={{ label: "Open graph explorer", onClick: () => navigate("/graph") }}
            />
          )}
        </Panel>

        <Panel
          mnemonic="ACTIVITY"
          description="latest agent tool calls"
          rail={activityFeed.some((a) => a.verdict === "deny") ? "err" : "ok"}
          live
        >
          {activityFeed.length === 0 ? (
            <EmptyState
              kind="none-yet"
              title="No recent activity"
              hint="Agent tool calls appear here as they are audited."
            />
          ) : (
            <DataTable
              caption="Most recent audited agent tool calls"
              columns={activityColumns}
              rows={activityFeed}
              rowKey={(a, i) => `${a.ts}-${i}`}
              defaultDensity="compact"
              showDensityToggle={false}
              minWidth={420}
              statusBar={false}
            />
          )}
        </Panel>
      </div>

      {/* The Intelligence Loop — the 5-stage pipeline strip, unchanged in
       *  meaning, now a Panel of StatTiles. Every metric comes from data this
       *  page already fetches; no new aggregate. */}
      <div style={{ marginTop: 16 }}>
        <Panel mnemonic="LOOP" description="trigger → collect → detect → notify → learn" rail="info">
          <div style={{ display: "grid", gridTemplateColumns: "repeat(5, 1fr)", gap: 12 }}>
            <StatTile label="Trigger" value={`${scanCount ?? 0} points`} sub="cron + webhooks" />
            <StatTile
              label="Collect"
              value={`${collectorCount} domains`}
              sub="reporting collectors"
            />
            <StatTile
              label="Detect"
              value={`${openDrift} drift`}
              color={openDrift > 0 ? "red" : "green"}
              sub="snapshot diffing"
            />
            <StatTile label="Notify" value={`${notifCount ?? 0} tickets`} sub="Jira + Confluence" />
            <StatTile
              label="Learn"
              value={`${counts.applied_instincts} instincts`}
              color={counts.invrec_proposed > 0 ? "yellow" : undefined}
              sub={
                <Link to="/invrec">
                  {counts.invrec_proposed} reconcile proposal
                  {counts.invrec_proposed === 1 ? "" : "s"} pending →
                </Link>
              }
            />
          </div>
        </Panel>
      </div>
    </main>
  );
}

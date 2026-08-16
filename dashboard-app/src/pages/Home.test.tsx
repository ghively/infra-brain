import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import * as api from "../api";
import { Home, buildDriftDays } from "./Home";
import type { DriftTrendPoint } from "./Home";

afterEach(() => {
  // vitest.config.ts sets `globals: false`, so @testing-library/react's
  // automatic afterEach(cleanup) registration never fires — without this,
  // renders from earlier tests in this file accumulate in the jsdom document
  // (see Drift.test.tsx / McpKeys.test.tsx for the same pattern).
  cleanup();
  vi.restoreAllMocks();
});

const BASE_COUNTS = {
  open_drift: 7,
  open_cves: 3,
  eol_overdue: 2,
  eol_total: 5,
  invrec_proposed: 1,
  invrec_total: 4,
  total_resources: 42,
  applied_instincts: 6,
};

/** `YYYY-MM-DD` for N days before today — the page buckets drift events into
 *  the last 7 *calendar* days off the wall clock, so fixtures that need to land
 *  inside the window must be relative, not hardcoded. */
function daysAgo(n: number): string {
  const d = new Date();
  d.setHours(0, 0, 0, 0);
  d.setDate(d.getDate() - n);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

/** `YYYY-MM-DD` for N days before today, in UTC — matches what the
 *  `/drift_events/trend` endpoint's `func.date()` aggregate actually returns
 *  (F-6/F-10: `buildDriftDays` now buckets in UTC, not local calendar days).
 *  Under the test environment's default UTC clock this equals `daysAgo()`,
 *  but is the semantically-correct fixture for `driftTrend` points. */
function daysAgoUTC(n: number): string {
  const now = new Date();
  const d = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() - n));
  return `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, "0")}-${String(d.getUTCDate()).padStart(2, "0")}`;
}

function mockHome(overrides: {
  counts?: Partial<typeof BASE_COUNTS>;
  drift?: unknown;
  /** `/drift_events/trend` response (F-10) — feeds the DRIFT tape + TREND
   *  chart. Separate from `drift` (the bounded `/drift_events` list, which
   *  now feeds only the DOMAIN table's per-domain open-drift counts). */
  driftTrend?: unknown;
  activity?: unknown;
  resources?: unknown;
  runs?: unknown;
  scanPoints?: unknown;
  notifications?: unknown;
  kgEdges?: unknown;
}) {
  const counts = { ...BASE_COUNTS, ...overrides.counts };
  vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
    if (url.includes("/counts")) return counts;
    // Check the more specific "/drift_events/trend" path before the generic
    // "/drift_events" one below, which would otherwise also match it.
    if (url.includes("/drift_events/trend"))
      return overrides.driftTrend ?? { points: [], total: 0, domain_filter: "", days: 7 };
    if (url.includes("/drift_events")) return overrides.drift ?? { items: [], total: 0 };
    if (url.includes("/activity")) return overrides.activity ?? { items: [], total: 0 };
    if (url.includes("/resources")) return overrides.resources ?? { items: [], total: 0 };
    if (url.includes("/collection_runs")) return overrides.runs ?? { items: [], total: 0 };
    if (url.includes("/scan_points")) return overrides.scanPoints ?? { items: [], total: 0 };
    if (url.includes("/notifications")) return overrides.notifications ?? { items: [], total: 0 };
    // Knowledge-graph adjacency for the GRAPH panel — a separate router from
    // /api/dashboard/*, and deliberately optional on this page.
    if (url.includes("/api/graph/kg/edges")) return overrides.kgEdges ?? { items: [], total: 0 };
    throw new Error(`unexpected apiGet: ${url}`);
  });
}

function LocationProbe() {
  return <div data-testid="pathname">{useLocation().pathname}</div>;
}

function renderHome() {
  return render(
    <MemoryRouter>
      <Home />
      <LocationProbe />
    </MemoryRouter>,
  );
}

describe("buildDriftDays (F-6 — UTC event dates vs. local-day bucket keys)", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.useRealTimers();
  });

  it("does not silently drop a UTC 'today' trend point relative to the operator's local day", () => {
    // Fixed instant: 2026-08-08T03:00:00Z. Under America/New_York (UTC-4 in
    // August, EDT) that instant is 2026-08-07 23:00 local — still "today"
    // for a UTC-5/UTC-4 operator, but already "2026-08-08" in UTC, which is
    // exactly the date the trend endpoint's `func.date()` aggregate (and
    // every `detected_at` string) is stamped with.
    vi.stubEnv("TZ", "America/New_York");
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-08-08T03:00:00Z"));

    const points: DriftTrendPoint[] = [{ date: "2026-08-08", count: 1, domain: "linux" }];
    const days = buildDriftDays(points);
    const total = days.reduce((a, d) => a + d.total, 0);

    // The point must land in SOME bucket — not vanish because the 7 bucket
    // keys were built from LOCAL calendar days (`new Date()` local getters)
    // that never contain the UTC key ("2026-08-08") the point is indexed
    // under — and it must land specifically in the LAST (today) bucket, not
    // a stale local-day one.
    expect(total).toBe(1);
    expect(days[days.length - 1].total).toBe(1);
  });
});

describe("Home page accuracy fixes (T1) — preserved across the Phase 2 rebuild", () => {
  it("A1: a 'failure'-status run surfaces the failed-collection-run signal", async () => {
    mockHome({
      runs: {
        items: [
          { status: "failure", started_at: "2026-07-24T00:00:00Z" },
          { status: "success", started_at: "2026-07-23T00:00:00Z" },
        ],
        total: 2,
      },
    });

    renderHome();

    await waitFor(() =>
      expect(screen.getByText(/1 failed collection run\b/)).toBeInTheDocument(),
    );
  });

  it("A1: a 'failed'-status run also surfaces it (tolerant, matching Topbar)", async () => {
    mockHome({
      runs: {
        items: [{ status: "failed", started_at: "2026-07-24T00:00:00Z" }],
        total: 1,
      },
    });

    renderHome();

    await waitFor(() =>
      expect(screen.getByText(/1 failed collection run\b/)).toBeInTheDocument(),
    );
  });

  it("A1: no failure/failed runs means no false positive", async () => {
    // Zero out every other attention source so the only thing that could
    // report a problem is a mis-detected failed run.
    mockHome({
      counts: { open_drift: 0, open_cves: 0, eol_overdue: 0, invrec_proposed: 0 },
      runs: {
        items: [{ status: "success", started_at: "2026-07-24T00:00:00Z" }],
        total: 1,
      },
    });

    renderHome();

    await waitFor(() => expect(screen.getByText("Open Drift")).toBeInTheDocument());
    expect(screen.queryByText(/failed collection run/)).not.toBeInTheDocument();
    expect(screen.getByText(/all runs healthy/)).toBeInTheDocument();
  });

  it("A2: Open Drift renders counts.open_drift, not a client-computed count", async () => {
    // Server aggregate says 7 open drift events total; the capped drift_events
    // fetch only returns 1 "open" row (simulating truncation past the page
    // cap). If the page were deriving the count client-side, it would render 1.
    mockHome({
      counts: { open_drift: 7 },
      drift: {
        items: [{ id: "1", domain: "linux", status: "open", detected_at: daysAgo(0) }],
        total: 1,
      },
    });

    renderHome();

    await waitFor(() => expect(screen.getByText("Open Drift")).toBeInTheDocument());
    // The SummaryBlock stat is the single owner of this number.
    expect(screen.getAllByText("7").length).toBeGreaterThanOrEqual(1);
    // The LOOP panel's Detect stage surfaces the same value in pipeline context.
    expect(screen.getByText("7 drift")).toBeInTheDocument();
  });

  it("A3: LOOP Trigger/Notify metrics use the response total, not the capped items length", async () => {
    // scan_points returns only 2 items but total=150 (page-cap truncation);
    // notifications returns 3 items but total=812.
    mockHome({
      scanPoints: { items: [{ domain: "linux" }, { domain: "k8s" }], total: 150 },
      notifications: { items: [{ id: "1" }, { id: "2" }, { id: "3" }], total: 812 },
    });

    renderHome();

    await waitFor(() => expect(screen.getByText("150 points")).toBeInTheDocument());
    expect(screen.getByText("812 tickets")).toBeInTheDocument();
  });
});

describe("Home page — Phase 2 design-system rebuild", () => {
  it("renders the hero fleet total and every drill-in link the old health cards had", async () => {
    mockHome({});
    renderHome();

    await waitFor(() => expect(screen.getByText("Fleet Resources")).toBeInTheDocument());
    expect(screen.getByText("42")).toBeInTheDocument();

    const hrefs = screen
      .getAllByRole("link")
      .map((a) => a.getAttribute("href"))
      .filter((h): h is string => Boolean(h));
    // /drift /vulns /eol /collruns /invrec were the five health-card targets;
    // /hosts is the hero's inventory drill-in.
    for (const path of ["/drift", "/vulns", "/eol", "/collruns", "/invrec", "/hosts"]) {
      expect(hrefs).toContain(path);
    }
  });

  it("builds the DOMAIN table from resources ⨝ drift_events and navigates on row click", async () => {
    mockHome({
      resources: {
        items: [
          { domain: "linux", status: "active" },
          { domain: "linux", status: "active" },
          { domain: "windows", status: "eol" },
          { domain: "k8s", status: "active" },
        ],
        total: 4,
      },
      drift: {
        items: [{ id: "d1", domain: "k8s", status: "open", detected_at: daysAgo(1) }],
        total: 1,
      },
    });

    renderHome();

    await waitFor(() => expect(screen.getByText("linux")).toBeInTheDocument());
    // Health precedence is unchanged from the old dot colors: EOL outranks
    // drift, drift outranks healthy.
    expect(screen.getByText("1 EOL")).toBeInTheDocument();
    expect(screen.getByText("1 open drift")).toBeInTheDocument();
    expect(screen.getByText("healthy")).toBeInTheDocument();

    fireEvent.click(screen.getByText("windows"));
    // domainPath("windows") → /hosts
    await waitFor(() => expect(screen.getByTestId("pathname")).toHaveTextContent("/hosts"));
  });

  it("DOMAIN panel: per-domain count/eol/open-drift and the health badge are labeled page-scoped", async () => {
    // Regression guard: unlike counts.open_drift (A2), there is no server-side
    // per-domain aggregate — `resources` is capped at ?limit=200 and `drift`
    // has no limit= param (server default limit=500). A domain whose EOL/drift
    // rows fall outside that page can render a false "healthy" badge, so the
    // panel must carry an explicit page-scope caveat (matching ResourcesTab.tsx's
    // "(page)" convention) rather than presenting these as authoritative totals.
    mockHome({
      resources: {
        items: [{ domain: "linux", status: "active" }],
        total: 1,
      },
      drift: {
        items: [{ id: "d1", domain: "linux", status: "open", detected_at: daysAgo(1) }],
        total: 1,
      },
    });

    renderHome();

    await waitFor(() => expect(screen.getByText("linux")).toBeInTheDocument());

    // Column headers carry the page-scope caveat.
    expect(screen.getByText("resources (page)")).toBeInTheDocument();
    expect(screen.getByText("open drift (page)")).toBeInTheDocument();
    expect(screen.getByText("eol (page)")).toBeInTheDocument();

    // The DataTable caption and toolbar chip carry it too.
    expect(
      screen.getByText(/Resource coverage and health by collection domain \(per-domain counts are page-scoped\)/),
    ).toBeInTheDocument();
    expect(screen.getByText("grouped by domain (page)")).toBeInTheDocument();

    // The panel description under the DOMAIN mnemonic carries it too (two
    // independent surfaces both say "page-scoped" by design — count them).
    expect(screen.getAllByText(/page-scoped/).length).toBeGreaterThanOrEqual(2);

    // The health badge's own tooltip — the thing driving the rail color — warns
    // the reading is a page-scoped sample, not an authoritative total.
    const badge = screen.getByText("1 open drift");
    expect(badge.getAttribute("title")).toMatch(/page-scoped sample/);
  });

  it("renders the 7-day TREND chart when drift exists and an EmptyState when it does not", async () => {
    // F-10: the chart now sources from /drift_events/trend, not the raw
    // /drift_events page.
    mockHome({
      driftTrend: {
        points: [
          { date: daysAgoUTC(0), count: 1, domain: "linux" },
          { date: daysAgoUTC(2), count: 1, domain: "linux" },
        ],
        total: 2,
        domain_filter: "",
        days: 7,
      },
    });
    renderHome();

    await waitFor(() =>
      expect(screen.getByLabelText(/Drift events trend over 7 periods/)).toBeInTheDocument(),
    );

    cleanup();
    vi.restoreAllMocks();

    mockHome({ driftTrend: { points: [], total: 0, domain_filter: "", days: 7 } });
    renderHome();
    await waitFor(() =>
      expect(screen.getByText("No drift events in the last 7 days")).toBeInTheDocument(),
    );
  });

  it("renders the drift tape instrument with the day's reading", async () => {
    // F-10: the tape's "today" reading now sources from /drift_events/trend.
    mockHome({
      driftTrend: {
        points: [{ date: daysAgoUTC(0), count: 2, domain: "linux" }],
        total: 2,
        domain_filter: "",
        days: 7,
      },
    });
    renderHome();

    await waitFor(() =>
      expect(screen.getByLabelText(/Drift events today: 2/)).toBeInTheDocument(),
    );
  });

  it("GRAPH panel: builds a real blast-radius view from /api/graph/kg/edges", async () => {
    // Graph-first P5: the panel moved off /api/graph/relationships (which read
    // resource_relationships and keyed its endpoints in the resources.id
    // space) onto the graph_edges census. Different id space, hence the
    // renamed fields — source_id/target_id/edge_type/source_name/target_name.
    mockHome({
      kgEdges: {
        items: [
          {
            source_id: "hub-1",
            target_id: "sat-1",
            edge_type: "HOSTED_ON",
            source_name: "esxi-01",
            target_name: "vm-app-01",
            confidence: 0.9,
          },
          {
            source_id: "hub-1",
            target_id: "sat-2",
            edge_type: "AFFECTED_BY_CVE",
            source_name: "esxi-01",
            target_name: "vm-db-01",
            confidence: 0.8,
          },
        ],
        total: 2,
      },
    });

    renderHome();

    await waitFor(() =>
      expect(
        screen.getByLabelText(/Relationship graph centered on esxi-01, with 2 related resources/),
      ).toBeInTheDocument(),
    );
    // AFFECTED_BY_CVE is risk-carrying → one flagged satellite + flagged edge.
    expect(screen.getByLabelText(/1 flagged, 1 flagged edge/)).toBeInTheDocument();
  });

  it("GRAPH panel: falls back to a clear EmptyState when the graph has no relationships", async () => {
    mockHome({ kgEdges: { items: [], total: 0 } });
    renderHome();

    await waitFor(() =>
      expect(screen.getByText("No graph relationships available")).toBeInTheDocument(),
    );
  });

  it("GRAPH panel: a failing graph endpoint degrades the panel, not the whole page", async () => {
    const counts = { ...BASE_COUNTS };
    vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
      if (url.includes("/api/graph/kg/edges")) throw new Error("graph router not deployed");
      if (url.includes("/counts")) return counts;
      return { items: [], total: 0 };
    });

    renderHome();

    await waitFor(() =>
      expect(screen.getByText("No graph relationships available")).toBeInTheDocument(),
    );
    // The rest of the page still rendered.
    expect(screen.getByText("Fleet Resources")).toBeInTheDocument();
  });

  it("ACTIVITY panel: renders the feed and marks a denied call", async () => {
    mockHome({
      activity: {
        items: [
          {
            ts: "2026-07-27T10:11:12Z",
            agent: "linux",
            tool: "ssh_read",
            verdict: "deny",
            latency_ms: 42,
          },
        ],
        total: 1,
      },
    });

    renderHome();

    await waitFor(() => expect(screen.getByText("ssh_read")).toBeInTheDocument());
    expect(screen.getByText("deny")).toBeInTheDocument();
    expect(screen.getByText("10:11:12")).toBeInTheDocument();
  });

  it("ACTIVITY panel: EmptyState when there is no recent activity", async () => {
    mockHome({ activity: { items: [], total: 0 } });
    renderHome();
    await waitFor(() => expect(screen.getByText("No recent activity")).toBeInTheDocument());
  });

  it("surfaces a load failure through the error EmptyState, not a bare div", async () => {
    vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
      if (url.includes("/counts")) throw new Error("boom");
      return { items: [], total: 0 };
    });

    renderHome();

    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
    expect(screen.getByText("Home failed to load")).toBeInTheDocument();
  });

  it("has a working Refresh control that re-issues the fetches", async () => {
    mockHome({});
    renderHome();

    await waitFor(() => expect(screen.getByText("Fleet Resources")).toBeInTheDocument());
    const spy = vi.mocked(api.apiGet);
    const before = spy.mock.calls.length;

    fireEvent.click(screen.getByRole("button", { name: /refresh/i }));

    await waitFor(() => expect(spy.mock.calls.length).toBeGreaterThan(before));
  });

  it("F-3: a refresh failure after a successful load keeps the populated dashboard on screen instead of unmounting it", async () => {
    mockHome({});
    renderHome();
    await waitFor(() => expect(screen.getByText("Fleet Resources")).toBeInTheDocument());

    // Simulate a transient 500 on ONE of the seven parallel fetches during the
    // next refresh — the rest still succeed.
    let scanPointsCalls = 0;
    vi.spyOn(api, "apiGet").mockImplementation(async (url: string) => {
      if (url.includes("/scan_points")) {
        scanPointsCalls++;
        throw new Error("500 on /scan_points");
      }
      if (url.includes("/counts")) return BASE_COUNTS;
      return { items: [], total: 0 };
    });

    fireEvent.click(screen.getByRole("button", { name: /refresh/i }));

    await waitFor(() => {
      expect(scanPointsCalls).toBeGreaterThanOrEqual(1);
      expect(screen.getByRole("button", { name: /refresh/i })).not.toHaveAttribute("aria-busy");
    });

    // The already-loaded dashboard must stay mounted — a single failing
    // endpoint on a background/manual refresh must not blank the whole page.
    expect(screen.getByText("Fleet Resources")).toBeInTheDocument();
    expect(screen.queryByText("Home failed to load")).not.toBeInTheDocument();
  });

  it("F-10: bounds the /activity fetch to what the feed actually renders (6 rows), not the 500-row server default", async () => {
    mockHome({});
    renderHome();
    await waitFor(() => expect(screen.getByText("Fleet Resources")).toBeInTheDocument());

    const spy = vi.mocked(api.apiGet);
    const activityCall = spy.mock.calls.find(([url]) => url.includes("/api/dashboard/activity"));
    expect(activityCall?.[0]).toMatch(/[?&]limit=6\b/);
  });

  it("F-10: sources the 7-day chart from the aggregate /drift_events/trend endpoint, not a full unbounded drift_events page", async () => {
    mockHome({});
    renderHome();
    await waitFor(() => expect(screen.getByText("Fleet Resources")).toBeInTheDocument());

    const spy = vi.mocked(api.apiGet);
    const calledTrend = spy.mock.calls.some(([url]) => url.includes("/api/dashboard/drift_events/trend"));
    expect(calledTrend).toBe(true);
  });

  it("F-10: the bounded drift_events fetch (domain breakdown) suppresses R7 telemetry noise, matching the Drift page's default view", async () => {
    mockHome({});
    renderHome();
    await waitFor(() => expect(screen.getByText("Fleet Resources")).toBeInTheDocument());

    const spy = vi.mocked(api.apiGet);
    const driftListCall = spy.mock.calls.find(([url]) => url.startsWith("/api/dashboard/drift_events?"));
    expect(driftListCall?.[0]).toMatch(/[?&]suppress_telemetry=true\b/);
    expect(driftListCall?.[0]).toMatch(/[?&]limit=\d+/);
  });
});

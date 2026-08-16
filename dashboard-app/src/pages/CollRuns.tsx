import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { apiGet, rows, AuthRequired, redirectToLogin } from "../api";
import { DetailDrawer } from "../components/DetailDrawer";
import { Skeleton } from "../components/Skeleton";
import { headerFor } from "../lib/headers";
import { useAutoRefresh } from "../hooks/useAutoRefresh";
import { fmtDt } from "../lib/fmtDt";
import { PageShell } from "../components/ui/PageShell";
import { Panel } from "../components/ui/Panel";
import { DataTable } from "../components/ui/DataTable";
import type { ColumnDef } from "../components/ui/DataTable";
import { Badge } from "../components/ui/SeverityPill";
import { SummaryBlock } from "../components/ui/StatTile";
import { EmptyState } from "../components/ui/EmptyState";
import { TrendBarChart } from "../components/ui/TrendBarChart";
import type { TrendBarDatum } from "../components/ui/TrendBarChart";
import { Tabs } from "../components/ui/Tabs";

// Port of dashboard/src/pages/collruns.dc.html + the collStats/collRunRows
// derivation (static/index.html:6109-6117) and the getCollChartData() bar-chart
// builder (static/index.html:5506-5528) from the legacy renderVals(). Backed by
// GET /api/dashboard/collection_runs (CollectionRunPageOut envelope,
// src/infra_brain/api/routers/fleet.py:121).
//
// Phase 3 design-foundation rebuild (dashboard-app ground-up redesign, see
// /home/youruser/.claude/plans/tidy-inventing-duckling.md): migrated from
// `PageHeader`/hand-rolled `<table>` onto the Phase 1 primitives (`PageShell`,
// `Panel`, `DataTable`, `SummaryBlock`) — every real feature/filter/interaction
// from the pre-rebuild page is preserved, including the three accuracy fixes
// noted below and the broadened partial-run error detection / 40-char error
// snippet / failure-pill-includes-retry_exhausted behaviors documented in
// docs/TRACKER.md.
//
// Row click opens a DetailDrawer showing the run detail (all run fields + an
// error block when the run failed/partial), matching the legacy detail:{kind:
// 'run'} drawer (static/index.html:3179-3237, JS 6904-6912). The run data is
// already in the fetched rows, so the clicked RunRow is passed straight into a
// page-local <RunDetail run=.../> — no extra fetch.
//
// Status → visual mapping (documented per this task's instruction): the four
// DataTable/Badge tones (ok/warn/err/info/neutral) don't map 1:1 onto the five+
// real run outcomes, so:
//   success            -> ok    (green)
//   partial            -> warn  (yellow) — distinct within the warning family
//                          via its own label text, not a fifth hue
//   failure            -> err   (red)
//   retry_exhausted    -> err   (red) — a failure-equivalent outcome (the
//                          RetryPolicy gave up rather than the run crashing
//                          outright), so it must read the same severity as a
//                          hard failure, not a softer one
//   running/queued     -> info  (blue)
//   skipped            -> neutral (muted grey)
//   anything else      -> neutral

type RunRow = {
  domain: string;
  trigger_type: string;
  status: string;
  records_collected: number;
  started_at: string;
  duration_seconds: number;
  error_message: string | null;
};

const STATUS_TABS: [string, string][] = [
  ["(all)", "All"],
  ["success", "Success"],
  ["failure", "Failure"],
  // partial passes through the backend's _RUN_STATUS map UNMAPPED (raw
  // "partial" -> display "partial") — see api/_helpers.py. It's a real,
  // distinct outcome (a degraded-but-not-fully-failed run, often with a
  // logged error_message) and previously had no tab/badge of its own, so it
  // rendered identically to a merely-still-running row under the generic
  // gray fallback. Preserved here.
  ["partial", "Partial"],
];

/** Status -> Badge tone. See the file-header docstring for the full mapping
 *  and rationale (retry_exhausted reads as a hard failure, partial stays in
 *  the warning family rather than inventing a fifth hue). */
function statusTone(status: string): "ok" | "warn" | "err" | "info" | "neutral" {
  switch (status) {
    case "success":
      return "ok";
    case "partial":
      return "warn";
    case "failure":
    case "retry_exhausted":
      return "err";
    case "running":
    case "queued":
      return "info";
    case "skipped":
      return "neutral";
    default:
      return "neutral";
  }
}

const filterLabelStyle: React.CSSProperties = {
  fontSize: 9,
  fontWeight: 700,
  color: "var(--ib-faint)",
  textTransform: "uppercase",
  letterSpacing: ".08em",
  marginRight: 2,
};

const detailLabelStyle: React.CSSProperties = {
  fontSize: 10,
  fontWeight: 700,
  textTransform: "uppercase",
  letterSpacing: ".08em",
  color: "var(--ib-faint)",
  marginBottom: 4,
};

/** Detail content for a single collection run, shown inside DetailDrawer.
 *  Port of the legacy detail:{kind:'run'} drawer body
 *  (static/index.html:3205-3235). */
function RunDetail({ run }: { run: RunRow }) {
  // Broadened to cover "partial" too — the API passes partial runs through
  // with error_message populated (a degraded run that still logged a real
  // error), and this check previously only recognized "failure", so a
  // partial run with a logged error silently showed no error block.
  const hasError = (run.status === "failure" || run.status === "partial") && !!run.error_message;

  const detailRow = (label: string, value: React.ReactNode) => (
    <div>
      <div style={detailLabelStyle}>{label}</div>
      <div className="ib-mono cell-primary">{value}</div>
    </div>
  );

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      <div>
        <Badge tone={statusTone(run.status)}>{run.status}</Badge>
      </div>
      {detailRow("Domain", run.domain)}
      {detailRow("Trigger", run.trigger_type)}
      {detailRow("Started", fmtDt(run.started_at))}
      {detailRow("Duration", `${run.duration_seconds}s`)}
      {detailRow("Records collected", run.records_collected)}

      {hasError && (
        <div>
          <div style={detailLabelStyle}>Error</div>
          <div
            style={{
              background: "rgba(240,101,78,.07)",
              border: "1px solid rgba(240,101,78,.25)",
              borderRadius: 10,
              padding: "14px 16px",
            }}
          >
            <div
              className="ib-mono"
              style={{ fontSize: 12, color: "var(--ib-red)", lineHeight: 1.6, whiteSpace: "pre-wrap", wordBreak: "break-word" }}
            >
              {run.error_message}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// Per-domain composition chart: not a literal time series, but the same
// "composition changing over a discrete axis" shape TrendBarChart is built
// for (DESIGN.md §8) — each bar is one domain's runs, split into a success
// base (`total`, drawn in green) and a failure highlight (`subset`, drawn in
// red on top). This is a like-for-like port of the legacy getCollChartData()
// stacked bar, just expressed through the shared widget instead of a
// page-local hand-rolled SVG-position calculator.
function buildDomainChart(runsData: RunRow[]): TrendBarDatum[] {
  const by: Record<string, { success: number; failure: number }> = {};
  runsData.forEach((r) => {
    if (!by[r.domain]) by[r.domain] = { success: 0, failure: 0 };
    if (r.status === "success" || r.status === "failure") {
      by[r.domain][r.status as "success" | "failure"] += 1;
    }
  });
  return Object.keys(by)
    .sort()
    .map((domain) => ({
      label: domain,
      total: by[domain].success + by[domain].failure,
      subset: by[domain].failure,
    }));
}

export function CollRuns() {
  const [runsData, setRunsData] = useState<RunRow[] | null>(null);
  // FE-6: true DB-wide run count from the {items,total,...} envelope, decoupled
  // from the capped page length. null until the first load resolves.
  const [totalRuns, setTotalRuns] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState("(all)");
  const [domain, setDomain] = useState("(all)");
  const [selected, setSelected] = useState<RunRow | null>(null);
  const triggerRef = useRef<HTMLElement>(null);

  // F-5: the status/domain filters are pushed to the backend
  // (fleet.py::list_runs already accepts `status=`/`domain=`) rather than
  // filtered in-memory over just the 100 most-recent runs. Filtering
  // client-side over that truncated window meant a domain that hasn't run
  // recently falsely showed "No collection runs match" even though matching
  // runs existed further back. Because `load` now depends on
  // `status`/`domain`, changing either triggers a real refetch below.
  const load = useCallback(async () => {
    const params = new URLSearchParams({ limit: "100" });
    if (status !== "(all)") params.set("status", status);
    if (domain !== "(all)") params.set("domain", domain);
    await apiGet<unknown>(`/api/dashboard/collection_runs?${params.toString()}`)
      .then((d) => {
        setRunsData(rows<RunRow>(d));
        setTotalRuns((d as { total?: number }).total ?? null);
        setError(null);
      })
      .catch((e) => {
        if (e instanceof AuthRequired) redirectToLogin();
        else setError(String(e));
      });
  }, [status, domain]);

  useEffect(() => {
    void load();
  }, [load]);
  useAutoRefresh(load);

  // Domain dropdown: populated from the live agent/domain roster (the same
  // DB-wide source Activity.tsx's agent filter dropdown uses), not derived
  // from the currently loaded run window — a domain whose most recent run
  // fell outside that window (or is filtered out by the current status tab)
  // must still be selectable. Sticky once populated: falls back to whatever
  // domains are in the loaded rows if the roster fetch fails, and never
  // shrinks its own options just because a status/domain filter narrowed the
  // loaded rows.
  const [rosterDomOpts, setRosterDomOpts] = useState<string[]>([]);
  useEffect(() => {
    let live = true;
    apiGet<unknown>("/api/dashboard/agents?limit=200")
      .then((d) => {
        if (!live) return;
        const doms = [...new Set(rows<{ domain: string }>(d).map((a) => a.domain))].filter(Boolean).sort();
        if (doms.length > 0) setRosterDomOpts(doms);
      })
      .catch(() => {
        /* cosmetic only — falls back to runsData-derived options below */
      });
    return () => {
      live = false;
    };
  }, []);

  const domOpts = useMemo(() => {
    if (rosterDomOpts.length > 0) return ["(all)", ...rosterDomOpts];
    return ["(all)", ...[...new Set((runsData ?? []).map((r) => r.domain))].sort()];
  }, [rosterDomOpts, runsData]);

  // rowsData is already server-filtered by the current status/domain
  // selection (see `load` above) — no client-side re-filtering needed.
  const filteredRuns = runsData ?? [];

  const chart = useMemo(() => buildDomainChart(runsData ?? []), [runsData]);

  const h = headerFor("collruns");

  if (error && runsData === null) {
    return (
      <div>
        <PageShell icon="📥" title={h.title} description={h.desc} />
        <EmptyState kind="error" title="Collection runs failed to load" hint={error} />
      </div>
    );
  }
  if (runsData === null) return <Skeleton count={3} height={32} />;

  const total = runsData.length;
  const success = runsData.filter((r) => r.status === "success").length;
  const failure = runsData.filter((r) => r.status === "failure").length;
  // ACCURACY (T2 audit): Success Rate's denominator used to be `total`, which
  // includes running/partial/skipped rows — non-terminal or degraded outcomes
  // that shouldn't be judged as either a success or a failure, diluting the
  // rate. Restrict the denominator to the two definite terminal outcomes
  // (success + failure). There's no /counts-style fleet-wide success/failure
  // aggregate for collection runs (fleet.py's CountsOut has none), so this
  // stays scoped to the loaded page — labeled "(page)" below rather than
  // presented as a true fleet-wide rate.
  const terminalTotal = success + failure;
  const successRate = terminalTotal ? Math.round((success / terminalTotal) * 100) : 0;
  // ACCURACY (T2 audit): an in-flight run (finished_at null) reports
  // duration_seconds=0 from the backend (see fleet.py's list_runs: `dur = 0`
  // when finished_at is unset) — that's a placeholder, not a real
  // near-instant run, and including it here deflated the average. Exclude
  // duration_seconds===0 && status==="running" rows from the average.
  const durationEligible = runsData.filter((r) => !(r.duration_seconds === 0 && r.status === "running"));
  const avgDuration = durationEligible.length
    ? Math.round(durationEligible.reduce((a, r) => a + (r.duration_seconds || 0), 0) / durationEligible.length)
    : 0;

  const pills = [
    { dot: "var(--ib-green)", text: `${success} succeeded` },
    { dot: "var(--ib-red)", text: `${failure} failed` },
  ];

  const columns: ColumnDef<RunRow>[] = [
    { key: "domain", header: "Domain", typeGlyph: "enum" },
    { key: "trigger_type", header: "Trigger", typeGlyph: "enum" },
    {
      key: "status",
      header: "Status",
      typeGlyph: "enum",
      render: (r) => <Badge tone={statusTone(r.status)}>{r.status}</Badge>,
    },
    { key: "records_collected", header: "Records", typeGlyph: "num" },
    {
      key: "started_at",
      header: "Started",
      typeGlyph: "text",
      render: (r) => <span className="ib-mono">{fmtDt(r.started_at)}</span>,
    },
    {
      key: "duration_seconds",
      header: "Duration",
      typeGlyph: "num",
      render: (r) => (r.duration_seconds === 0 && r.status === "running" ? "—" : `${r.duration_seconds}s`),
    },
    {
      key: "error_message",
      header: "Error",
      typeGlyph: "text",
      sortable: false,
      render: (r) => {
        // Broadened to cover "partial" — see RunDetail's hasError above.
        const hasErr = (r.status === "failure" || r.status === "partial") && !!r.error_message;
        if (!hasErr) return <span style={{ color: "var(--ib-faint)" }}>—</span>;
        const em = String(r.error_message);
        const snippet = em.length > 40 ? em.slice(0, 40) + "…" : em;
        return (
          <span
            className="ib-mono"
            title={em}
            style={{
              color: "var(--ib-red)",
              maxWidth: 220,
              display: "inline-block",
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
              verticalAlign: "bottom",
            }}
          >
            {snippet}
          </span>
        );
      },
    },
  ];

  return (
    <div>
      <PageShell icon="📥" title={h.title} description={h.desc} pills={pills} />

      {error && (
        <Panel rail="err" mnemonic="ERROR" description="Could not refresh — showing last known results.">
          <div style={{ padding: "6px 2px", fontSize: 12, color: "var(--ib-red)" }}>{error}</div>
        </Panel>
      )}

      <div style={{ marginBottom: 16 }}>
        <Panel mnemonic="RUNS" description="fleet-wide collection run summary">
          <div style={{ padding: "14px 2px 2px" }}>
            <SummaryBlock
              hero={{ label: "Total Runs", value: String(totalRuns ?? total) }}
              stats={[
                { label: "Success Rate (page)", value: `${successRate}%`, color: "green" },
                { label: "Failures", value: String(failure), color: failure ? "red" : undefined },
                { label: "Avg Duration", value: `${avgDuration}s`, color: "blue" },
              ]}
            />
          </div>
        </Panel>
      </div>

      <div style={{ marginBottom: 16 }}>
        <Panel mnemonic="TREND" description="runs by domain — success (base) vs. failure (highlighted)">
          {chart.length === 0 ? (
            <EmptyState kind="none-yet" title="No runs collected yet." />
          ) : (
            <div style={{ padding: "12px 4px" }}>
              <TrendBarChart
                data={chart}
                totalColor="var(--ib-green)"
                subsetColor="var(--ib-red)"
                legend={{ totalLabel: "Runs", subsetLabel: "Failures" }}
                height={120}
              />
            </div>
          )}
        </Panel>
      </div>

      <Panel
        mnemonic="COLLRUNS"
        description="filters"
        headerRight={
          <div style={{ display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
            <span style={filterLabelStyle}>Status</span>
            <Tabs
              tabs={STATUS_TABS.map(([v, label]) => ({ key: v, label }))}
              active={status}
              onChange={setStatus}
              label="Collection run status"
            />
            <div style={{ width: 1, height: 16, background: "var(--ib-border)", margin: "0 4px" }} />
            <span style={filterLabelStyle}>Domain</span>
            <select
              value={domain}
              onChange={(e) => setDomain(e.target.value)}
              style={{
                background: "var(--ib-raised)",
                border: "1px solid var(--ib-border)",
                borderRadius: 7,
                padding: "5px 10px",
                color: "var(--ib-text)",
                fontSize: 12,
                fontFamily: "inherit",
                outline: "none",
                cursor: "pointer",
              }}
            >
              {domOpts.map((o) => (
                <option key={o} value={o}>
                  {o}
                </option>
              ))}
            </select>
          </div>
        }
      >
        {filteredRuns.length === 0 ? (
          <EmptyState
            kind="filter-zero"
            title="No collection runs match the current filters."
            hint="Try clearing the status or domain filter."
          />
        ) : (
          <DataTable<RunRow>
            columns={columns}
            rows={filteredRuns}
            rowKey={(r, i) => `${r.domain}-${r.started_at}-${i}`}
            sortable
            onRowClick={(r) => setSelected(r)}
            caption="Collection runs"
            statusBar={{ shown: filteredRuns.length, total: totalRuns ?? total }}
          />
        )}
      </Panel>

      <DetailDrawer
        title={selected ? `Collection Run · ${selected.domain}` : "Collection run detail"}
        open={selected !== null}
        onClose={() => setSelected(null)}
        triggerRef={triggerRef}
      >
        {selected && <RunDetail run={selected} />}
      </DetailDrawer>
    </div>
  );
}

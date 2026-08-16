import { useCallback, useEffect, useMemo, useState } from "react";
import { apiGet, AuthRequired, redirectToLogin } from "../api";
import { Skeleton } from "../components/Skeleton";
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

// Phase 4 Task 4 sweep view. Backed by GET /api/dashboard/sweeps and
// GET /api/dashboard/sweeps/{sweep_id} (src/infra_brain/api/routers/fleet.py).
// Each sweep groups CollectionRun rows by sweep_id; the detail view drills
// into the per-domain rows, grouped into the three sweep-graph tiers
// (collector / reconciler / reasoner — see etl/spec.py's Tier enum).
// REPORTER/ON_DEMAND/unknown-tier domains fall into a fourth "other" column
// so nothing silently disappears from the grid.
//
// Phase 3 design-foundation rebuild (dashboard-app ground-up redesign, see
// /home/youruser/.claude/plans/tidy-inventing-duckling.md): migrated from
// `PageHeader`/hand-rolled `<table>`s onto the Phase 1 primitives (`PageShell`,
// `Panel`, `DataTable`, `SummaryBlock`) plus a `TrendBarChart` widget. Every
// real feature/filter/interaction from the pre-rebuild page is preserved,
// including the accuracy fixes already in place: `partial`/`skipped` counted
// in the per-sweep chips (previously silently dropped), `retry_exhausted`
// counted toward the page's failed total, and null-duration (in-flight)
// sweeps excluded from the average-duration stat rather than treated as 0.
//
// Status → visual mapping (documented per this task's instruction — same
// axis, same reasoning as CollRuns.tsx): the seven real per-domain statuses
// collapse onto the five Badge/status tones as:
//   completed          -> ok    (green)
//   partial            -> warn  (yellow) — stays in the warning family
//   failed             -> err   (red)
//   retry_exhausted    -> err   (red) — failure-equivalent, reads the same
//                          severity as `failed`, not softer
//   interrupt_pending  -> info  (blue) — awaiting a human decision, not a
//                          failure; there is no fifth "pending" hue to spend
//                          on it, and blue is the informational catch-all
//   in_progress        -> info  (blue)
//   skipped            -> neutral (muted grey)

type StatusCounts = {
  completed: number;
  partial: number;
  failed: number;
  retry_exhausted: number;
  interrupt_pending: number;
  in_progress: number;
  skipped: number;
};

type SweepSummary = {
  sweep_id: string;
  started_at: string | null;
  finished_at: string | null;
  duration_seconds: number | null;
  domain_count: number;
  status_counts: StatusCounts;
  max_iters_hit_count: number;
};

type SweepList = { items: SweepSummary[]; total: number; limit: number };

type SweepDomain = {
  domain: string;
  tier: string;
  status: string;
  started_at: string | null;
  finished_at: string | null;
  duration_seconds: number | null;
  records_collected: number;
  error_message: string | null;
  max_iters_hits: number;
};

type SweepDetail = {
  sweep_id: string;
  started_at: string | null;
  finished_at: string | null;
  duration_seconds: number | null;
  status_counts: StatusCounts;
  max_iters_hit_count: number;
  domains: SweepDomain[];
};

// This page reads CollectionRun rows grouped by sweep_id (fleet.py's
// get_sweeps()), and sweep_id is only ever minted by the sweep-graph
// orchestrator (graph.py::run_sweep_sync, invoked from webhooks.py only when
// `sweep_graph_enabled` is True). That flag defaults False in config.py, so
// on a stock install this page is *expected* to stay empty even though
// collection is working fine via the classic per-domain dispatch path
// (supervisor.py::dispatch(), which never sets sweep_id at all) — the
// frontend has no endpoint to read the flag's live value, so this is phrased
// as a conditional ("if it's off") rather than an assertion of the exact
// runtime state.
const SWEEPS_NOT_CONFIGURED_HINT =
  "This view only has data when the experimental sweep-graph orchestrator (sweep_graph_enabled) is turned on — it defaults to off. If it's off here, that's expected, not a failure: Collection Runs has the same per-domain run history without needing that flag.";

const TIER_COLUMNS: [string, string][] = [
  ["collector", "Collectors"],
  ["reconciler", "Reconcilers"],
  ["reasoner", "Reasoners"],
];

function statusTone(status: string): "ok" | "warn" | "err" | "info" | "neutral" {
  switch (status) {
    case "completed":
      return "ok";
    case "partial":
      return "warn";
    case "failed":
    case "retry_exhausted":
      return "err";
    case "interrupt_pending":
    case "in_progress":
      return "info";
    case "skipped":
      return "neutral";
    default:
      return "neutral";
  }
}

// Compact label for each status-count field, used by CountChips below —
// consolidates what were 6 near-empty numeric columns into one chip cell.
// ACCURACY (T2 audit, preserved): "partial" and "skipped" are included even
// though earlier code omitted them — StatusCounts/the backend's status_counts
// always includes them (see _SWEEP_STATUSES in fleet.py), and a sweep with
// partial or skipped domains must not silently drop those domains from every
// visible chip (the row's chips should sum to its Domains total).
const COUNT_FIELDS: [keyof StatusCounts, string][] = [
  ["completed", "done"],
  ["partial", "partial"],
  ["failed", "failed"],
  ["retry_exhausted", "retry-exh"],
  ["interrupt_pending", "interrupt"],
  ["in_progress", "active"],
  ["skipped", "skipped"],
];

/** Compact status-summary chips for a sweep row — shows only the non-zero
 *  counts instead of one mostly-empty column per status. The row itself is
 *  never dropped/hidden when every count is zero; it renders an em-dash
 *  (legitimately "nothing to report" for that sweep, not missing data). */
function CountChips({ counts, maxIters }: { counts: StatusCounts; maxIters: number }) {
  const nonZero = COUNT_FIELDS.filter(([key]) => counts[key] > 0);
  if (nonZero.length === 0 && maxIters === 0) {
    return <span style={{ fontSize: 12, color: "var(--ib-faint)" }}>—</span>;
  }
  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
      {nonZero.map(([key, label]) => (
        <Badge key={key} tone={statusTone(key)}>
          {label} {counts[key]}
        </Badge>
      ))}
      {maxIters > 0 && (
        <Badge tone="warn">
          max-iter {maxIters}
        </Badge>
      )}
    </div>
  );
}

function fmtDuration(s: number | null) {
  if (s === null || s === undefined) return "—";
  return `${s}s`;
}

/** Per-tier domain grouping for the selected sweep's detail view. */
function TierGrid({ domains }: { domains: SweepDomain[] }) {
  const byTier: Record<string, SweepDomain[]> = {};
  for (const d of domains) {
    const key = TIER_COLUMNS.some(([t]) => t === d.tier) ? d.tier : "other";
    (byTier[key] ??= []).push(d);
  }
  const columns = [...TIER_COLUMNS, ["other", "Other / Unknown"] as [string, string]].filter(
    ([tier]) => (byTier[tier] ?? []).length > 0,
  );

  if (columns.length === 0) {
    return <EmptyState kind="none-yet" title="No domains in this sweep." />;
  }

  return (
    <div style={{ display: "grid", gridTemplateColumns: `repeat(${columns.length}, 1fr)`, gap: 14 }}>
      {columns.map(([tier, label]) => {
        const tierDomains = byTier[tier] ?? [];
        const rail = tierDomains.some((d) => d.status === "failed" || d.status === "retry_exhausted")
          ? "err"
          : tierDomains.some((d) => d.status === "partial")
            ? "warn"
            : "ok";
        return (
          <Panel key={tier} rail={rail} mnemonic={label.toUpperCase()} description={`${tierDomains.length} domain${tierDomains.length === 1 ? "" : "s"}`}>
            <div style={{ display: "flex", flexDirection: "column", gap: 8, padding: "10px 2px 2px" }}>
              {tierDomains.map((d) => (
                <div
                  key={d.domain}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "space-between",
                    gap: 8,
                    padding: "8px 10px",
                    borderRadius: "var(--ib-radius-sm)",
                    background: "var(--ib-raised)",
                    border: "1px solid var(--ib-border)",
                  }}
                >
                  <div style={{ minWidth: 0 }}>
                    <div style={{ fontSize: 13, fontWeight: 500, color: "var(--ib-text)", overflow: "hidden", textOverflow: "ellipsis" }}>
                      {d.domain}
                    </div>
                    <div style={{ fontSize: 11, color: "var(--ib-muted)" }}>
                      {d.records_collected} records · {fmtDuration(d.duration_seconds)}
                      {d.max_iters_hits > 0 ? ` · ${d.max_iters_hits} max-iter hit${d.max_iters_hits > 1 ? "s" : ""}` : ""}
                    </div>
                    {d.error_message ? (
                      <div
                        className="ib-mono"
                        title={d.error_message}
                        style={{ fontSize: 11, color: "var(--ib-red)", marginTop: 2, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}
                      >
                        {d.error_message}
                      </div>
                    ) : null}
                  </div>
                  <Badge tone={statusTone(d.status)}>{d.status}</Badge>
                </div>
              ))}
            </div>
          </Panel>
        );
      })}
    </div>
  );
}

// Per-sweep composition chart, oldest -> newest: each bar is one recent
// sweep's domain_count (`total`, drawn as the base fill) with its failed +
// retry_exhausted domains highlighted on top (`subset`) — the same
// TrendBarChart adaptation CollRuns.tsx uses (DESIGN.md §8's "composition
// changing over time"), and here it genuinely *is* time-ordered since sweeps
// are chronological. Reversed because /api/dashboard/sweeps returns most
// recent first.
function buildSweepChart(items: SweepSummary[]): TrendBarDatum[] {
  return [...items]
    .reverse()
    .map((s) => ({
      label: s.started_at ? s.started_at.slice(5, 10) : s.sweep_id.slice(0, 8),
      total: s.domain_count,
      subset: s.status_counts.failed + s.status_counts.retry_exhausted,
    }));
}

export function Sweeps() {
  const [list, setList] = useState<SweepList | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<SweepDetail | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);

  const load = useCallback(async () => {
    await apiGet<SweepList>("/api/dashboard/sweeps?limit=20")
      .then((d) => {
        setList(d);
        setSelected((prev) => prev ?? (d.items.length > 0 ? d.items[0].sweep_id : null));
        setError(null);
      })
      .catch((e) => {
        if (e instanceof AuthRequired) redirectToLogin();
        else setError(String(e));
      });
  }, []);

  useEffect(() => {
    void load();
  }, [load]);
  useAutoRefresh(load);

  useEffect(() => {
    if (!selected) {
      setDetail(null);
      return;
    }
    setDetail(null);
    setDetailError(null);
    apiGet<SweepDetail>(`/api/dashboard/sweeps/${selected}`)
      .then(setDetail)
      .catch((e) => {
        if (e instanceof AuthRequired) redirectToLogin();
        else setDetailError(String(e));
      });
  }, [selected]);

  const stats = useMemo(() => {
    const items = list?.items ?? [];
    const totalSweeps = items.length;
    const totalMaxIters = items.reduce((a, s) => a + s.max_iters_hit_count, 0);
    const totalDomains = items.reduce((a, s) => a + s.domain_count, 0);
    const maxItersRate = totalDomains ? Math.round((totalMaxIters / totalDomains) * 1000) / 10 : 0;
    // ACCURACY (T2 audit, preserved): an in-flight sweep reports
    // duration_seconds: null, which `|| 0` used to silently treat as a real
    // zero — deflating the average while still counting the sweep in the
    // denominator. Exclude null-duration (in-flight) sweeps from both the
    // sum and the count.
    const withDuration = items.filter(
      (s): s is SweepSummary & { duration_seconds: number } =>
        s.duration_seconds !== null && s.duration_seconds !== undefined,
    );
    const avgDuration = withDuration.length
      ? Math.round(withDuration.reduce((a, s) => a + s.duration_seconds, 0) / withDuration.length)
      : 0;
    return { totalSweeps, totalMaxIters, maxItersRate, avgDuration };
  }, [list]);

  const chart = useMemo(() => buildSweepChart(list?.items ?? []), [list]);

  if (error && list === null) {
    return (
      <div>
        <PageShell
          icon="🧹"
          title="Sweeps"
          description="Registry-driven sweep runs grouped by sweep_id, broken out per graph tier (collector / reconciler / reasoner)."
        />
        <EmptyState kind="error" title="Sweeps failed to load" hint={error} />
      </div>
    );
  }
  if (list === null) return <Skeleton count={3} height={32} />;

  const succeeded = list.items.reduce((a, s) => a + s.status_counts.completed, 0);
  // ACCURACY (T2 audit, preserved): retry_exhausted is a failure-equivalent
  // status per api/_helpers.py's _RUN_STATUS map (RetryPolicy exhausted —
  // swallowed rather than crashing the sweep); it must count toward "failed"
  // here or a sweep could show "0 failed" while retry-exhausted domains exist.
  const failedTotal = list.items.reduce(
    (a, s) => a + s.status_counts.failed + s.status_counts.retry_exhausted,
    0,
  );
  const pills = [
    { dot: "var(--ib-green)", text: `${succeeded} succeeded` },
    { dot: "var(--ib-red)", text: `${failedTotal} failed` },
  ];

  const columns: ColumnDef<SweepSummary>[] = [
    {
      key: "sweep_id",
      header: "Sweep",
      typeGlyph: "pk",
      render: (s) => (
        <span
          className="ib-mono"
          title={s.sweep_id}
          style={{ color: s.sweep_id === selected ? "var(--ib-blue)" : "var(--ib-text)", fontWeight: s.sweep_id === selected ? 600 : 400 }}
        >
          {s.sweep_id === selected ? "▸ " : ""}
          {s.sweep_id.slice(0, 8)}
        </span>
      ),
    },
    {
      key: "started_at",
      header: "Started",
      typeGlyph: "text",
      render: (s) => <span className="ib-mono">{fmtDt(s.started_at)}</span>,
    },
    {
      key: "duration_seconds",
      header: "Duration",
      typeGlyph: "num",
      render: (s) => fmtDuration(s.duration_seconds),
    },
    { key: "domain_count", header: "Domains", typeGlyph: "num" },
    {
      key: "counts",
      header: "Counts",
      typeGlyph: "enum",
      sortable: false,
      render: (s) => <CountChips counts={s.status_counts} maxIters={s.max_iters_hit_count} />,
    },
  ];

  return (
    <div>
      <PageShell
        icon="🧹"
        title="Sweeps"
        description="Registry-driven sweep runs grouped by sweep_id, broken out per graph tier (collector / reconciler / reasoner)."
        pills={pills}
      />

      {error && (
        <Panel rail="err" mnemonic="ERROR" description="Could not refresh — showing last known results.">
          <div style={{ padding: "6px 2px", fontSize: 12, color: "var(--ib-red)" }}>{error}</div>
        </Panel>
      )}

      <div style={{ marginBottom: 16 }}>
        <Panel mnemonic="SWEEPS" description="recent sweep summary">
          <div style={{ padding: "14px 2px 2px" }}>
            <SummaryBlock
              hero={{ label: "Sweeps Shown", value: String(stats.totalSweeps) }}
              stats={[
                { label: "Avg Duration", value: `${stats.avgDuration}s`, color: "blue" },
                { label: "Max-Iters Hits", value: String(stats.totalMaxIters), color: stats.totalMaxIters ? "yellow" : undefined },
                { label: "Max-Iters Hit Rate", value: `${stats.maxItersRate}%`, color: stats.maxItersRate ? "yellow" : undefined },
              ]}
            />
          </div>
        </Panel>
      </div>

      <div style={{ marginBottom: 16 }}>
        <Panel mnemonic="TREND" description="domains per sweep — completed (base) vs. failed/retry-exhausted (highlighted)">
          {chart.length === 0 ? (
            <EmptyState
              kind="not-configured"
              title="No sweep-graph runs recorded"
              hint={SWEEPS_NOT_CONFIGURED_HINT}
            />
          ) : (
            <div style={{ padding: "12px 4px" }}>
              <TrendBarChart
                data={chart}
                totalColor="var(--ib-green)"
                subsetColor="var(--ib-red)"
                legend={{ totalLabel: "Domains", subsetLabel: "Failed" }}
                height={120}
              />
            </div>
          )}
        </Panel>
      </div>

      <div style={{ marginBottom: 18 }}>
        <Panel mnemonic="RUNS" description="click a sweep to view its per-tier breakdown">
          {list.items.length === 0 ? (
            <EmptyState
              kind="not-configured"
              title="No sweep-graph runs recorded"
              hint={SWEEPS_NOT_CONFIGURED_HINT}
            />
          ) : (
            <DataTable<SweepSummary>
              columns={columns}
              rows={list.items}
              rowKey={(s) => s.sweep_id}
              onRowClick={(s) => setSelected(s.sweep_id)}
              caption="Sweeps"
              statusBar={{ shown: list.items.length, total: list.total }}
            />
          )}
        </Panel>
      </div>

      {selected ? (
        <div>
          <div style={{ fontSize: 14, fontWeight: 600, color: "var(--ib-text)", marginBottom: 12 }}>
            Sweep {selected.slice(0, 8)} — per-tier domain status
          </div>
          {detailError ? (
            <EmptyState kind="error" title="Sweep detail failed to load" hint={detailError} />
          ) : detail === null ? (
            <Skeleton count={2} height={100} />
          ) : (
            <TierGrid domains={detail.domains} />
          )}
        </div>
      ) : null}
    </div>
  );
}

import { useCallback, useEffect, useMemo, useState } from "react";
import { apiGet, rows, AuthRequired, redirectToLogin } from "../api";
import { Skeleton } from "../components/Skeleton";
import { headerFor } from "../lib/headers";
import { useAutoRefresh } from "../hooks/useAutoRefresh";
import { PageShell, StatTile, Panel, DataTable, EmptyState, type ColumnDef } from "../components/ui";
import { fmtDt } from "../lib/fmtDt";

type Observation = {
  agent: string;
  tool: string;
  domain: string;
  pattern: string;
  count: number;
  last_seen: string;
};

/** Port of legacy observations.dc.html. Backed by GET /api/dashboard/observations
 *  (governance.py:290) — returns ObservationPageOut {items,total,limit,offset},
 *  ordered by count desc server-side already; sort client-side too to match the
 *  legacy `obsRows` derivation defensively.
 *
 *  Rebuilt on the Phase 1 design system (see dashboard-app/DESIGN.md) — Phase 3
 *  mechanical PageShell/StatTile/Panel/DataTable conversion. Preserves the
 *  count-desc client-side sort (kept as `initialSortKey`/`initialSortDir` on
 *  `DataTable` so a user can still re-sort by any column) and the 4-stat
 *  summary row (Patterns / Total Calls / Agents / Top Count). */
export function Observations() {
  const [obs, setObs] = useState<Observation[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    await apiGet<unknown>("/api/dashboard/observations?limit=200")
      .then((d) => setObs(rows<Observation>(d)))
      .catch((e) => {
        if (e instanceof AuthRequired) redirectToLogin();
        else setError(String(e));
      });
  }, []);

  useEffect(() => {
    void load();
  }, [load]);
  useAutoRefresh(load);

  const obsRows = useMemo(() => (obs ? [...obs].sort((a, b) => b.count - a.count) : []), [obs]);

  const stats = useMemo(() => {
    const all = obs || [];
    return [
      { label: "Patterns", value: all.length, color: undefined },
      { label: "Total Calls", value: all.reduce((a, o) => a + o.count, 0), color: "blue" as const },
      { label: "Agents", value: new Set(all.map((o) => o.agent)).size, color: undefined },
      { label: "Top Count", value: all.length ? Math.max(...all.map((o) => o.count)) : 0, color: "green" as const },
    ];
  }, [obs]);

  const h = headerFor("observations");

  if (error) {
    return (
      <div>
        <PageShell title={h.title} description={h.desc} />
        <EmptyState kind="error" title="Observations failed to load" hint={error} />
      </div>
    );
  }
  if (obs === null) {
    return (
      <div>
        <PageShell title={h.title} description={h.desc} />
        <Skeleton count={3} height={32} />
      </div>
    );
  }

  const empty = obsRows.length === 0;

  const columns: ColumnDef<Observation>[] = [
    { key: "agent", header: "Agent", typeGlyph: "text" },
    { key: "tool", header: "Tool", typeGlyph: "text" },
    { key: "pattern", header: "Observed Pattern", typeGlyph: "text" },
    { key: "count", header: "Count", typeGlyph: "num" },
    { key: "last_seen", header: "Last Seen", typeGlyph: "text", render: (o) => fmtDt(o.last_seen) },
  ];

  return (
    <div>
      <PageShell title={h.title} description={h.desc} />

      <div style={{ display: "grid", gridTemplateColumns: "repeat(4,1fr)", gap: 12, marginBottom: 16 }}>
        {stats.map((s) => (
          <StatTile key={s.label} label={s.label} value={s.value} color={s.color} />
        ))}
      </div>

      <Panel mnemonic="OBS" description="observations — tool-usage patterns" live>
        {empty ? (
          <EmptyState
            kind="none-yet"
            title="No observations recorded yet"
            hint="Observations appear here as agent tool calls are recorded by the ObservationCallbackHandler."
          />
        ) : (
          <DataTable<Observation>
            columns={columns}
            rows={obsRows}
            rowKey={(o, i) => `${o.agent}-${o.tool}-${i}`}
            sortable
            initialSortKey="count"
            initialSortDir="desc"
            toolbarName="observations"
            caption="Observations: agent, tool, observed pattern, count, and last seen"
            statusBar={{ shown: obsRows.length, note: "read-only · no mutations possible from this view" }}
          />
        )}
      </Panel>
    </div>
  );
}

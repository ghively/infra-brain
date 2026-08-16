import { useCallback, useEffect, useState } from "react";
import { apiGet, rows, AuthRequired, redirectToLogin } from "../api";
import { Skeleton } from "../components/Skeleton";
import { headerFor } from "../lib/headers";
import { fmtDt } from "../lib/fmtDt";
import { useAutoRefresh } from "../hooks/useAutoRefresh";
import {
  PageShell,
  StatTile,
  Panel,
  DataTable,
  Button,
  EmptyState,
  Badge,
  type ColumnDef,
  type PanelRail,
} from "../components/ui";

// Port of dashboard/src/pages/activity.dc.html + the actStats/actVerdictTabs/
// filteredAct derivation in the legacy renderVals() (static/index.html:6141-6167).
// Backed by GET /api/dashboard/activity (ActivityPageOut envelope,
// src/infra_brain/api/routers/governance.py:370). Rebuilt on the Phase 1
// design system (see dashboard-app/DESIGN.md) as a Phase 3 mechanical
// DataTable/Panel conversion — see Security.tsx/NetDiscovery.tsx for the
// established pattern this follows.

type ActivityRow = {
  ts: string;
  agent: string;
  domain: string;
  tool: string;
  verdict: string;
  status: string;
  latency_ms: number;
  args_summary: string;
  error: string;
};

// Fallback list, used only if the live-roster fetch below fails — kept in
// sync with supervisor.py's AGENT_REGISTRY as of 2026-07-24 so a roster-fetch
// outage still yields a correct (if unmaintained-going-forward) dropdown
// instead of the previous permanently-stale list (which still had "net",
// disabled since the 2026-07-15 POC scoping, and was missing ~16 live
// domains). The live path below is what actually stays accurate.
const FALLBACK_AGENT_OPTS = [
  "(all)",
  "linux",
  "windows",
  "cicd",
  "octopus",
  "knowledge",
  "local_docs",
  "vuln",
  "eol",
  "fleet_health",
  "iac",
  "vsphere",
  "drift",
  "notification",
  "discovery",
  "drift_learning",
  "inventory_reconcile",
  "remediation",
  "vuln_triage",
  "compliance",
  "rootcause",
  "learning_feedback",
  "host_reconcile",
  "netdiscovery",
  "inventory_mr",
  "coverage",
  "graph_maintenance",
  "query",
];

type Filter = "(all)" | "allow" | "deny";

const VERDICT_TABS: { v: Filter; label: string }[] = [
  { v: "(all)", label: "All" },
  { v: "allow", label: "Allowed" },
  { v: "deny", label: "Denied" },
];

/** Verdict → Badge tone. `allow`/`deny` map onto the same ok/err hues used
 *  elsewhere for a pass/fail verdict (DESIGN.md §3's Status axis); anything
 *  else is neutral rather than guessed. */
function verdictTone(v: string): "ok" | "err" | "neutral" {
  if (v === "allow") return "ok";
  if (v === "deny") return "err";
  return "neutral";
}

export function Activity() {
  const [rowsData, setRowsData] = useState<ActivityRow[] | null>(null);
  // FE: true DB-wide tool-call total from the {items,total,...} envelope —
  // the 500-row window (`rowsData.length`) previously stood in for this on
  // the "Tool Calls" tile, silently capping the displayed count at 500.
  const [totalCalls, setTotalCalls] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [agent, setAgent] = useState("(all)");
  const [verdict, setVerdict] = useState<Filter>("(all)");
  // Agent-filter dropdown, derived from the live agent roster
  // (/api/dashboard/agents, the same registry-backed source Agents.tsx uses)
  // rather than a hand-maintained copy of supervisor.py's AGENT_REGISTRY —
  // the previous hardcoded list had drifted (stale "net", missing ~16 live
  // domains). Falls back to a manually-updated snapshot if the roster fetch
  // fails; the fallback list is cosmetic only (filtering still works against
  // whatever value is selected).
  const [agentOpts, setAgentOpts] = useState<string[]>(FALLBACK_AGENT_OPTS);

  // F-5: the agent/verdict filters are pushed to the backend (governance_audit.py's
  // list_activity already accepts `agent=`/`verdict=`, matching the pattern
  // Agents.tsx already uses) rather than filtered in-memory over just the 500
  // most-recent rows. Filtering client-side over that truncated window meant
  // picking an agent whose last tool calls fell outside it produced a false
  // "No activity entries match the current filters" even though matching
  // rows existed. Because `load` now depends on `agent`/`verdict`, changing
  // either triggers a real refetch (see the effect below).
  const load = useCallback(async () => {
    const params = new URLSearchParams({ limit: "500" });
    if (agent !== "(all)") params.set("agent", agent);
    if (verdict !== "(all)") params.set("verdict", verdict);
    await apiGet<unknown>(`/api/dashboard/activity?${params.toString()}`)
      .then((d) => {
        setRowsData(rows<ActivityRow>(d));
        setTotalCalls((d as { total?: number }).total ?? null);
      })
      .catch((e) => {
        if (e instanceof AuthRequired) redirectToLogin();
        else setError(String(e));
      });
  }, [agent, verdict]);

  useEffect(() => {
    void load();
  }, [load]);
  useAutoRefresh(load);

  useEffect(() => {
    let live = true;
    apiGet<unknown>("/api/dashboard/agents?limit=200")
      .then((d) => {
        if (!live) return;
        const domains = [...new Set(rows<{ domain: string }>(d).map((a) => a.domain))].filter(Boolean).sort();
        if (domains.length > 0) setAgentOpts(["(all)", ...domains]);
      })
      .catch(() => {
        /* option list is cosmetic — FALLBACK_AGENT_OPTS above still lets filtering work */
      });
    return () => {
      live = false;
    };
  }, []);

  const h = headerFor("activity");

  if (error) {
    return (
      <div>
        <PageShell title={h.title} description={h.desc} />
        <EmptyState kind="error" title="Activity failed to load" hint={error} />
      </div>
    );
  }

  if (rowsData === null) {
    return (
      <div>
        <PageShell title={h.title} description={h.desc} />
        <Skeleton count={3} height={32} />
      </div>
    );
  }

  // rowsData is already server-filtered by the current agent/verdict
  // selection (see `load` above) — no client-side re-filtering needed, and
  // none is applied, so a match outside the old 500-row window is no longer
  // silently dropped.
  const filtered = rowsData;

  // (page): these counts, like the table itself, are bounded by the fetched
  // window (limit=500) — capped-aggregate labeling convention, see
  // CollRuns.tsx's "Success Rate (page)".
  const allowed = rowsData.filter((a) => a.verdict === "allow").length;
  const denied = rowsData.filter((a) => a.verdict === "deny").length;
  const avgLatency = rowsData.length
    ? Math.round(rowsData.reduce((sum, a) => sum + (a.latency_ms || 0), 0) / rowsData.length)
    : 0;

  const stats: Array<{ label: string; value: number | string; color: "red" | "yellow" | "green" | "blue" | undefined }> = [
    // DB-wide count matching the current agent/verdict filter (from the
    // {items,total,...} envelope's `total`, computed server-side before the
    // limit/offset slice) — not capped to the fetched window.
    { label: "Tool Calls", value: totalCalls ?? rowsData.length, color: undefined },
    { label: "Allowed (page)", value: allowed, color: "green" },
    { label: "Denied (page)", value: denied, color: "red" },
    { label: "Avg Latency", value: `${avgLatency}ms`, color: "blue" },
  ];

  // Panel rail matches the worst finding in the visible (filtered) window —
  // same "rail matches worst contained finding" rule as every other Panel
  // (DESIGN.md §6): any denied call reads red, else green.
  const rail: PanelRail = filtered.some((a) => a.verdict === "deny") ? "err" : "ok";

  const hasAnyFilter = agent !== "(all)" || verdict !== "(all)";
  const empty = filtered.length === 0;

  const columns: ColumnDef<ActivityRow>[] = [
    {
      key: "ts",
      header: "Timestamp",
      typeGlyph: "text",
      render: (a) => {
        const t = fmtDt(a.ts);
        return t ? <span style={{ whiteSpace: "nowrap" }}>{t}</span> : undefined;
      },
    },
    { key: "agent", header: "Agent", typeGlyph: "enum" },
    {
      key: "tool",
      header: "Tool",
      typeGlyph: "text",
      render: (a) => <span style={{ whiteSpace: "nowrap" }}>{a.tool}</span>,
    },
    {
      key: "verdict",
      header: "Verdict",
      typeGlyph: "enum",
      render: (a) => <Badge tone={verdictTone(a.verdict)}>{a.verdict}</Badge>,
    },
    {
      key: "latency_ms",
      header: "Latency",
      typeGlyph: "num",
      render: (a) => `${a.latency_ms}ms`,
    },
    {
      key: "args_summary",
      header: "Args / Reason",
      typeGlyph: "text",
      sortable: false,
      render: (a) => {
        const isDeny = a.verdict === "deny";
        const text = isDeny ? a.error : a.args_summary;
        return (
          <span
            style={{
              color: isDeny ? "var(--ib-red)" : "var(--ib-faint)",
              maxWidth: 260,
              display: "inline-block",
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
            title={text}
          >
            {text}
          </span>
        );
      },
    },
  ];

  return (
    <div>
      <PageShell title={h.title} description={h.desc} />

      <div style={{ display: "grid", gridTemplateColumns: "repeat(4,1fr)", gap: 12, marginBottom: 16 }}>
        {stats.map((s) => (
          <StatTile key={s.label} label={s.label} value={s.value} color={s.color} />
        ))}
      </div>

      <div style={{ display: "flex", gap: 6, marginBottom: 16, flexWrap: "wrap", alignItems: "center" }}>
        {VERDICT_TABS.map((t) => (
          <Button key={t.v} variant={verdict === t.v ? "primary" : "ghost"} size="sm" onClick={() => setVerdict(t.v)}>
            {t.label}
          </Button>
        ))}
        <select
          value={agent}
          onChange={(e) => setAgent(e.target.value)}
          style={{
            marginLeft: "auto",
            background: "var(--ib-raised)",
            border: "1px solid var(--ib-border)",
            borderRadius: "var(--ib-radius-sm)",
            padding: "6px 12px",
            color: "var(--ib-text)",
            fontSize: 13,
            fontFamily: "inherit",
            outline: "none",
            cursor: "pointer",
          }}
        >
          {agentOpts.map((o) => (
            <option key={o} value={o}>
              {o}
            </option>
          ))}
        </select>
      </div>

      <Panel rail={rail} mnemonic="ACTIVITY" description="tool call log — safety callback chain" live>
        {empty ? (
          hasAnyFilter ? (
            <EmptyState
              kind="filter-zero"
              title="No activity entries match the current filters"
              hint="Try a different tab, or clear the agent filter."
              action={{
                label: "Clear filters",
                onClick: () => {
                  setAgent("(all)");
                  setVerdict("(all)");
                },
              }}
            />
          ) : (
            <EmptyState
              kind="none-yet"
              title="No activity recorded"
              hint="Entries will appear here once agent tool calls are made."
            />
          )
        ) : (
          <DataTable<ActivityRow>
            columns={columns}
            rows={filtered}
            rowKey={(a, i) => `${a.ts}-${i}`}
            sortable
            initialSortKey="ts"
            initialSortDir="desc"
            toolbarName="activity"
            caption="Activity log: timestamp, agent, tool, verdict, latency, and args/reason"
            statusBar={{
              shown: filtered.length,
              total: totalCalls ?? rowsData.length,
              note: "read-only · no mutations possible from this view",
            }}
          />
        )}
      </Panel>
    </div>
  );
}

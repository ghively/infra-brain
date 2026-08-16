import { useCallback, useEffect, useRef, useState } from "react";
import { apiGet, rows, AuthRequired, redirectToLogin } from "../api";
import { DetailDrawer } from "../components/DetailDrawer";
import { Skeleton } from "../components/Skeleton";
import { headerFor } from "../lib/headers";
import { Icon } from "../lib/icons";
import { useAutoRefresh } from "../hooks/useAutoRefresh";
import {
  PageShell,
  StatTile,
  Panel,
  DataTable,
  EmptyState,
  Badge,
  type ColumnDef,
} from "../components/ui";
import { fmtDt } from "../lib/fmtDt";

// Port of dashboard/src/pages/agents.dc.html + the agentsView/agentStats
// derivation in the legacy renderVals() (static/index.html:6297-6303), and the
// agent detail drawer (static/index.html:3057-3173 template, :6950-6966 JS).
// Backed by GET /api/dashboard/agents (AgentRosterPageOut envelope,
// src/infra_brain/api/routers/governance.py:702). Rebuilt on the Phase 1
// design system (see dashboard-app/DESIGN.md) as a Phase 3 mechanical
// DataTable/Panel conversion — see Security.tsx/CollRuns.tsx for the
// established pattern this follows.
//
// The prior card grid became a DataTable: name/domain/kind/schedule/last
// run/status are homogeneous fields shared by every agent row (DESIGN.md §8 —
// table is the default for >5 homogeneous records), and row click still opens
// the same DetailDrawer as before, matching the legacy detail:{kind:'agent',
// data:a} pattern (T1.3).
//
// Card click opens a DetailDrawer. The drawer shows description, configuration,
// read-only tool chips, recent tool calls (with verdict badges) and recent
// decisions. Legacy loads the full /activity and /decisions collections
// globally then filters client-side by `x.agent === a.domain`
// (static/index.html:6960-6962) — this port fetches the same two endpoints
// when the drawer opens and applies the identical filter, scoped server-side
// via the endpoints' own `agent=` query param (see AgentDetail below).

type AgentRow = {
  name: string;
  domain: string;
  kind: string;
  schedule: string;
  last_run: string;
  status: string;
  output: string;
  desc: string;
  tools: string[];
};

type ActivityCall = {
  ts: string;
  agent: string;
  tool: string;
  verdict: string;
  latency_ms: number;
};

type Decision = {
  agent: string;
  run_id: string;
  iteration: number;
  decision_summary: string;
  ts: string;
};

/** Kind → Badge tone. Only four semantic hues exist (DESIGN.md §2), so this is
 *  a documented, arbitrary-but-fixed categorical assignment rather than a
 *  state judgment: `collector` (the majority fleet member) reads `ok`/green,
 *  `llm` (the reasoning-tier agents) reads `info`/blue, and everything else
 *  (`system`) reads `neutral`/grey. */
function kindTone(k: string): "ok" | "info" | "neutral" {
  if (k === "collector") return "ok";
  if (k === "llm") return "info";
  return "neutral";
}

// T8: "partial" (a run that reported success but a detail-write failed —
// see governance_ops.py's list_agents status map) and "skipped" (dependency
// unconfigured / collection_disabled_domains) must never fall through to the
// same grey as "idle" ("no runs yet") — that made both look like harmless
// never-run agents. "partial" gets the same warn tone as "degraded" (partial
// data IS a degraded state); "skipped" gets its own distinct `info` tone so an
// intentional pause reads differently from both a real problem and an agent
// that has simply never run.
// "retired" (AgentSpec.retired — an enterprise-remnant collector switched off
// by standing decision, see docs/ARCHITECTURE.md) deliberately takes the SAME
// neutral tone as "idle". Badge only has five hues and the four meaningful ones
// are already spoken for; of the collisions available, sharing grey with "never
// run" is the least misleading — both mean "this agent is not producing data
// and that is not an alarm". What must NOT collide is retired vs "skipped":
// skipped means "dependency unconfigured, go configure it", retired means "not
// coming back unless you decide otherwise", and the info tone keeps them apart.
// The badge renders the literal status string, so "retired" and "idle" still
// read differently even at the same hue.
function statusTone(status: string): "ok" | "warn" | "info" | "neutral" {
  if (status === "healthy") return "ok";
  if (status === "degraded" || status === "partial") return "warn";
  if (status === "skipped") return "info";
  if (status === "retired") return "neutral";
  return "neutral";
}

function kindLabel(k: string): string {
  return k === "llm" ? "LLM agent" : k === "system" ? "system agent" : "collector";
}

const SECTION_LABEL_STYLE: React.CSSProperties = {
  fontSize: 10,
  fontWeight: 700,
  color: "var(--ib-muted)",
  textTransform: "uppercase",
  letterSpacing: ".1em",
  marginBottom: 12,
  fontFamily: "var(--ib-mono, 'DM Mono', monospace)",
};

/** Detail content for a single agent, shown inside DetailDrawer. Fetches the
 *  recent tool calls (/activity) and decisions (/decisions) on open, scoped
 *  server-side to this agent's domain via the endpoints' own `agent=` query
 *  param (governance_audit.py) rather than fetching the global feed and
 *  filtering client-side — the old client-side filter operated over just the
 *  most recent 500 rows *across every agent*, so a low-frequency agent's
 *  real history outside that window silently read as "no calls". */
function AgentDetail({ agent }: { agent: AgentRow }) {
  const [calls, setCalls] = useState<ActivityCall[] | null>(null);
  const [callsError, setCallsError] = useState<string | null>(null);
  const [decisions, setDecisions] = useState<Decision[] | null>(null);
  const [decisionsError, setDecisionsError] = useState<string | null>(null);
  const tools = agent.tools ?? [];

  useEffect(() => {
    let cancelled = false;
    setCalls(null);
    setCallsError(null);
    setDecisions(null);
    setDecisionsError(null);
    void apiGet<unknown>(`/api/dashboard/activity?agent=${encodeURIComponent(agent.domain)}&limit=5`)
      .then((d) => {
        if (cancelled) return;
        setCalls(rows<ActivityCall>(d));
      })
      .catch((e) => {
        // Drawer is supplementary — a feed error here shouldn't blank the
        // whole drawer, but it must be visibly distinct from "this agent
        // made no calls" rather than silently rendering as empty.
        if (!cancelled) setCallsError(String(e));
      });
    void apiGet<unknown>(`/api/dashboard/decisions?agent=${encodeURIComponent(agent.domain)}&limit=4`)
      .then((d) => {
        if (cancelled) return;
        setDecisions(rows<Decision>(d));
      })
      .catch((e) => {
        if (!cancelled) setDecisionsError(String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [agent.domain]);

  const cfgRow = (label: string, value: React.ReactNode, mono = false) => (
    <div
      style={{
        display: "flex",
        justifyContent: "space-between",
        padding: "11px 14px",
        background: "var(--ib-raised)",
      }}
    >
      <span style={{ fontSize: 12, color: "var(--ib-muted)" }}>{label}</span>
      <span
        style={{
          fontSize: 12,
          color: "var(--ib-text)",
          fontFamily: mono ? "var(--ib-mono, 'DM Mono', monospace)" : undefined,
        }}
      >
        {value}
      </span>
    </div>
  );

  return (
    <div style={{ padding: "4px 0" }}>
      {/* Header: kind label + name + status/kind pills */}
      <div style={{ marginBottom: 22 }}>
        <div
          style={{
            fontSize: 11,
            fontWeight: 600,
            color: "var(--ib-muted)",
            textTransform: "uppercase",
            letterSpacing: ".08em",
            marginBottom: 6,
          }}
        >
          {kindLabel(agent.kind)}
        </div>
        <div
          style={{
            fontSize: 17,
            fontWeight: 700,
            color: "var(--ib-text)",
            fontFamily: "var(--ib-mono, 'DM Mono', monospace)",
            wordBreak: "break-all",
          }}
        >
          {agent.name}
        </div>
        <div style={{ display: "flex", gap: 8, marginTop: 10, flexWrap: "wrap" }}>
          <Badge tone={statusTone(agent.status)}>{agent.status}</Badge>
          <Badge tone={kindTone(agent.kind)}>{agent.kind}</Badge>
        </div>
      </div>

      {agent.desc && (
        <div style={{ fontSize: 13, color: "var(--ib-muted)", lineHeight: 1.6, marginBottom: 22 }}>{agent.desc}</div>
      )}

      <div style={SECTION_LABEL_STYLE}>Configuration</div>
      <div
        style={{
          display: "flex",
          flexDirection: "column",
          gap: 1,
          background: "var(--ib-border)",
          borderRadius: "var(--ib-radius)",
          overflow: "hidden",
          marginBottom: 24,
        }}
      >
        {cfgRow("Domain", agent.domain)}
        {cfgRow("Schedule", agent.schedule, true)}
        {cfgRow("Last run", fmtDt(agent.last_run) ?? "—", true)}
        {cfgRow("Last output", agent.output)}
      </div>

      <div style={SECTION_LABEL_STYLE}>Tools (read-only)</div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 7, marginBottom: 24 }}>
        {tools.length === 0 && <span style={{ fontSize: 11, color: "var(--ib-faint)" }}>—</span>}
        {tools.map((t) => (
          <span
            key={t}
            style={{
              fontSize: 11,
              fontWeight: 500,
              padding: "3px 10px",
              borderRadius: "var(--ib-radius-sm)",
              background: "var(--ib-raised)",
              color: "var(--ib-blue)",
              border: "1px solid var(--ib-border)",
              fontFamily: "var(--ib-mono, 'DM Mono', monospace)",
            }}
          >
            {t}
          </span>
        ))}
      </div>

      {/* Recent Tool Calls — a fetch failure gets its own visible message
       *  instead of silently rendering as "made no calls" (T-011). */}
      <div style={SECTION_LABEL_STYLE}>Recent Tool Calls</div>
      {callsError ? (
        <div style={{ fontSize: 12, color: "var(--ib-red)", marginBottom: 24 }}>Failed to load: {callsError}</div>
      ) : calls === null ? (
        <div style={{ marginBottom: 24 }}>
          <Skeleton count={2} height={40} />
        </div>
      ) : calls.length === 0 ? (
        <div style={{ fontSize: 12, color: "var(--ib-faint)", marginBottom: 24 }}>
          No tool calls recorded for this agent.
        </div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 8, marginBottom: 24 }}>
          {calls.map((c, i) => (
            <div
              key={`${c.ts}-${c.tool}-${i}`}
              style={{
                background: "var(--ib-raised)",
                border: "1px solid var(--ib-border)",
                borderRadius: 9,
                padding: "11px 13px",
                display: "flex",
                alignItems: "center",
                gap: 10,
              }}
            >
              <span
                style={{
                  fontSize: 11,
                  fontFamily: "var(--ib-mono, 'DM Mono', monospace)",
                  color: "var(--ib-blue)",
                  flex: 1,
                  minWidth: 0,
                  whiteSpace: "nowrap",
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                }}
              >
                {c.tool}
              </span>
              <span style={{ fontSize: 10, color: "var(--ib-faint)", fontFamily: "var(--ib-mono, 'DM Mono', monospace)" }}>
                {c.latency_ms}ms
              </span>
              <Badge tone={c.verdict === "allow" ? "ok" : c.verdict === "deny" ? "err" : "neutral"}>{c.verdict}</Badge>
            </div>
          ))}
        </div>
      )}

      <div style={SECTION_LABEL_STYLE}>Recent Decisions</div>
      {decisionsError ? (
        <div style={{ fontSize: 12, color: "var(--ib-red)" }}>Failed to load: {decisionsError}</div>
      ) : decisions === null ? (
        <Skeleton count={2} height={44} />
      ) : decisions.length === 0 ? (
        <div style={{ fontSize: 12, color: "var(--ib-faint)" }}>No reasoning decisions recorded for this agent.</div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {decisions.map((d, i) => (
            <div
              key={`${d.run_id}-${d.iteration}-${i}`}
              style={{
                background: "var(--ib-raised)",
                border: "1px solid var(--ib-border)",
                borderRadius: 9,
                padding: "12px 14px",
              }}
            >
              <div style={{ fontSize: 12, fontWeight: 600, color: "var(--ib-text)", marginBottom: 4 }}>
                {d.decision_summary}
              </div>
              <div style={{ fontSize: 11, color: "var(--ib-faint)", fontFamily: "var(--ib-mono, 'DM Mono', monospace)" }}>
                {d.run_id} · iter {d.iteration} · {fmtDt(d.ts) ?? d.ts}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export function Agents() {
  const [rowsData, setRowsData] = useState<AgentRow[] | null>(null);
  // FE-6: true DB-wide roster count from the {items,total,...} envelope.
  const [totalAgents, setTotalAgents] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<AgentRow | null>(null);
  const triggerRef = useRef<HTMLElement>(null);

  const load = useCallback(async () => {
    await apiGet<unknown>("/api/dashboard/agents?limit=200")
      .then((d) => {
        setRowsData(rows<AgentRow>(d));
        setTotalAgents((d as { total?: number }).total ?? null);
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

  const h = headerFor("agents");

  if (error) {
    return (
      <div>
        <PageShell icon={<Icon name={h.icon} />} title={h.title} description={h.desc} />
        <EmptyState kind="error" title="Agents failed to load" hint={error} />
      </div>
    );
  }

  if (rowsData === null) {
    return (
      <div>
        <PageShell icon={<Icon name={h.icon} />} title={h.title} description={h.desc} />
        <Skeleton count={3} height={32} />
      </div>
    );
  }

  const pills = [
    { dot: "var(--ib-green)", text: `${rowsData.filter((a) => a.kind === "collector").length} collectors` },
    { dot: "var(--ib-blue)", text: `${rowsData.filter((a) => a.kind === "llm").length} LLM agents` },
    {
      dot: "var(--ib-faint)",
      text: `${rowsData.filter((a) => a.kind !== "collector" && a.kind !== "llm").length} system`,
    },
  ];

  const healthyCount = rowsData.filter((a) => a.status === "healthy").length;
  // T8: "partial" (detail-write failure on an otherwise-successful run) is
  // folded into the Degraded tile — partial data is a degraded state, and
  // without this a fleet of silently-incomplete collectors would show
  // Degraded: 0 with no visible explanation for Healthy + Degraded < Total.
  const degradedCount = rowsData.filter((a) => a.status === "degraded" || a.status === "partial").length;

  const stats: Array<{ label: string; value: number; color: "red" | "yellow" | "green" | "blue" | undefined }> = [
    { label: "Total Agents", value: totalAgents ?? rowsData.length, color: undefined },
    { label: "Collectors", value: rowsData.filter((a) => a.kind === "collector").length, color: "green" },
    { label: "Healthy", value: healthyCount, color: "green" },
    { label: "Degraded", value: degradedCount, color: "yellow" },
  ];

  const empty = rowsData.length === 0;

  const columns: ColumnDef<AgentRow>[] = [
    { key: "name", header: "Name", typeGlyph: "pk" },
    { key: "domain", header: "Domain", typeGlyph: "enum" },
    {
      key: "kind",
      header: "Kind",
      typeGlyph: "enum",
      render: (a) => <Badge tone={kindTone(a.kind)}>{a.kind}</Badge>,
    },
    { key: "schedule", header: "Schedule", typeGlyph: "text" },
    {
      key: "last_run",
      header: "Last Run",
      typeGlyph: "text",
      render: (a) => fmtDt(a.last_run) ?? "—",
    },
    {
      key: "status",
      header: "Status",
      typeGlyph: "enum",
      render: (a) => <Badge tone={statusTone(a.status)}>{a.status}</Badge>,
    },
  ];

  return (
    <div>
      <PageShell icon={<Icon name={h.icon} />} title={h.title} description={h.desc} pills={pills} />

      <div style={{ display: "grid", gridTemplateColumns: "repeat(4,1fr)", gap: 12, marginBottom: 16 }}>
        {stats.map((s) => (
          <StatTile key={s.label} label={s.label} value={s.value} color={s.color} />
        ))}
      </div>

      <Panel mnemonic="AGENTS" description="agent registry — supervisor.py AGENT_REGISTRY" live>
        {empty ? (
          <EmptyState
            kind="none-yet"
            title="No agents registered"
            hint="Agents appear here once registered in the agent registry."
          />
        ) : (
          <DataTable<AgentRow>
            columns={columns}
            rows={rowsData}
            rowKey={(a) => a.name}
            sortable
            initialSortKey="name"
            onRowClick={(a) => setSelected(a)}
            toolbarName="agents"
            caption="Agent registry: name, domain, kind, schedule, last run, and status"
            statusBar={{
              shown: rowsData.length,
              total: totalAgents ?? rowsData.length,
              note: "read-only · no mutations possible from this view",
            }}
          />
        )}
      </Panel>

      <DetailDrawer
        title={selected ? selected.name : "Agent detail"}
        open={selected !== null}
        onClose={() => setSelected(null)}
        triggerRef={triggerRef}
      >
        {selected && <AgentDetail agent={selected} />}
      </DetailDrawer>
    </div>
  );
}

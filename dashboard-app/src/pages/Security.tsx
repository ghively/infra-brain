import { useCallback, useEffect, useState } from "react";
import { apiGet, rows, AuthRequired, redirectToLogin } from "../api";
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
import { Skeleton } from "../components/Skeleton";
import { statusColor } from "../lib/tokens";

type AuditEntry = {
  ts: string;
  agent: string;
  tool: string;
  category: string;
  allowed: boolean;
  reason: string;
  ihash: string;
  ohash: string;
};

type Filter = "(all)" | "denied" | "dlp" | "allowed";

const TABS: { v: Filter; label: string }[] = [
  { v: "(all)", label: "All" },
  { v: "denied", label: "Denied" },
  { v: "dlp", label: "DLP Blocks" },
  { v: "allowed", label: "Allowed" },
];

/** Category ("Layer") → Badge tone + label. The legacy page hand-picked a
 *  distinct purple hue per layer; the Phase 1 design system caps the palette
 *  at four semantic hues (DESIGN.md §2), so `dlp` maps to `warn` (a DLP
 *  suppression is a safety-layer finding worth reviewing, distinct from an
 *  outright `denied` verdict which is already `err`/red) and `allowlist`
 *  maps to `info` (blue, the informational/interactive hue). Anything else
 *  (the structural `readonly` layer) is `neutral`. */
const CAT_TONE: Record<string, { tone: "neutral" | "info" | "ok" | "warn" | "err"; label: string }> = {
  dlp: { tone: "warn", label: "DLP" },
  allowlist: { tone: "info", label: "allow-list" },
};

function catInfo(c: string) {
  return CAT_TONE[c] ?? { tone: "neutral" as const, label: "read-only" };
}

/** Port of the legacy Security page (dashboard/src/pages/security.dc.html),
 *  rebuilt on the Phase 1 design system (see dashboard-app/DESIGN.md).
 *  Backed by GET /api/dashboard/audit_log ({items,total,...} envelope,
 *  AuditPageOut — src/infra_brain/api/routers/governance_audit.py:113). The
 *  legacy page fetched the full set once and filtered client-side via tabs;
 *  this port keeps that behavior for the DRILL-DOWN TABLE (fetch the
 *  most-recent-200 window once, filter client-side) to preserve exact tab
 *  semantics ("dlp" tab = category==='dlp' regardless of allowed).
 *
 *  The STAT TILES and header pills, however, must show true totals, not a
 *  count within that 200-row window (bug found in the 2026-07-24 overnight
 *  audit — "Audited Calls" etc. silently capped at 200 once the log grew
 *  past it). The backend already supports server-side `category`/`allowed`
 *  filters with an accurate `total` in the envelope even when `items` is
 *  windowed, so true counts are fetched via two cheap (`limit=1`) sibling
 *  requests: unfiltered total, and `allowed=false` for the true denied
 *  total. `allowed` is a non-nullable column (every row is allowed XOR
 *  denied — src/infra_brain/db/models/core.py's AuditLog.allowed), so the
 *  true "Allowed" total is derivable as total - denied without a third
 *  request. The DLP total needs its own `category=dlp` request since it is
 *  not complementary to allowed/denied.
 */
export function Security() {
  const [entries, setEntries] = useState<AuditEntry[] | null>(null);
  const [totalCount, setTotalCount] = useState<number | null>(null);
  const [deniedCount, setDeniedCount] = useState<number | null>(null);
  const [dlpCount, setDlpCount] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<Filter>("(all)");

  const load = useCallback(async () => {
    await Promise.all([
      apiGet<unknown>("/api/dashboard/audit_log").then((d) => {
        setEntries(rows<AuditEntry>(d));
        setTotalCount((d as { total?: number }).total ?? null);
      }),
      apiGet<unknown>("/api/dashboard/audit_log?allowed=false&limit=1").then((d) => {
        setDeniedCount((d as { total?: number }).total ?? null);
      }),
      apiGet<unknown>("/api/dashboard/audit_log?category=dlp&limit=1").then((d) => {
        setDlpCount((d as { total?: number }).total ?? null);
      }),
    ]).catch((e) => {
      if (e instanceof AuthRequired) redirectToLogin();
      else setError(String(e));
    });
  }, []);

  useEffect(() => {
    void load();
  }, [load]);
  useAutoRefresh(load);

  const h = headerFor("security");

  if (error) {
    return (
      <div>
        <PageShell title={h.title} description={h.desc} />
        <EmptyState kind="error" title="Security audit log failed to load" hint={error} />
      </div>
    );
  }

  if (entries === null) {
    return (
      <div>
        <PageShell title={h.title} description={h.desc} />
        <Skeleton count={3} height={32} />
      </div>
    );
  }

  // True totals when the sibling count requests succeeded; fall back to the
  // 200-row window only if one of them came back without a `total` (e.g. an
  // older/mismatched backend) rather than showing nothing.
  const totalAudited = totalCount ?? entries.length;
  const totalDenied = deniedCount ?? entries.filter((a) => !a.allowed).length;
  const totalDlp = dlpCount ?? entries.filter((a) => a.category === "dlp").length;
  const totalAllowed =
    totalCount !== null && deniedCount !== null ? totalCount - deniedCount : entries.filter((a) => a.allowed).length;

  const pills = [
    { dot: statusColor("ok"), text: `${totalAllowed} allowed` },
    { dot: statusColor("error"), text: `${totalDenied} denied` },
    { dot: statusColor("warn"), text: `${totalDlp} DLP block` },
  ];

  const stats: Array<{ label: string; value: number; color: "red" | "yellow" | "green" | "blue" | undefined }> = [
    { label: "Audited Calls", value: totalAudited, color: undefined },
    { label: "Allowed", value: totalAllowed, color: "green" },
    { label: "Denied", value: totalDenied, color: "red" },
    { label: "DLP Blocks", value: totalDlp, color: "yellow" },
  ];

  const filtered = entries.filter(
    (a) =>
      filter === "(all)" ||
      (filter === "denied" && !a.allowed) ||
      (filter === "dlp" && a.category === "dlp") ||
      (filter === "allowed" && a.allowed),
  );

  // Panel rail matches the worst finding in the visible (filtered) window:
  // any denied call reads red, else any DLP block reads yellow, else green
  // — same "rail matches worst contained finding" rule as every other Panel
  // (DESIGN.md §6), applied to verdict/category instead of a severity field.
  const rail: PanelRail = filtered.some((a) => !a.allowed) ? "err" : filtered.some((a) => a.category === "dlp") ? "warn" : "ok";

  const hasAnyFilter = filter !== "(all)";
  const noDataAtAll = entries.length === 0;
  const empty = filtered.length === 0;

  const columns: ColumnDef<AuditEntry>[] = [
    {
      key: "ts",
      header: "Timestamp",
      typeGlyph: "text",
      render: (a) => <span style={{ whiteSpace: "nowrap" }}>{fmtDt(a.ts)}</span>,
    },
    { key: "agent", header: "Agent", typeGlyph: "fk" },
    {
      key: "tool",
      header: "Tool",
      typeGlyph: "text",
      render: (a) => <span style={{ whiteSpace: "nowrap" }}>{a.tool}</span>,
    },
    {
      key: "category",
      header: "Layer",
      typeGlyph: "enum",
      render: (a) => {
        const cat = catInfo(a.category);
        return <Badge tone={cat.tone}>{cat.label}</Badge>;
      },
    },
    {
      key: "allowed",
      header: "Verdict",
      typeGlyph: "enum",
      render: (a) => <Badge tone={a.allowed ? "ok" : "err"}>{a.allowed ? "allowed" : "denied"}</Badge>,
    },
    {
      key: "reason",
      header: "Reason",
      typeGlyph: "text",
      render: (a) => <span style={{ color: a.allowed ? "var(--ib-muted)" : statusColor("error") }}>{a.reason}</span>,
    },
    {
      key: "ihash",
      header: "Hashes",
      typeGlyph: "text",
      sortable: false,
      render: (a) => (
        <span
          style={{ fontFamily: "var(--ib-mono, 'DM Mono', monospace)", color: "var(--ib-faint)", whiteSpace: "nowrap" }}
          title={`${a.ihash} → ${a.ohash}`}
        >
          {String(a.ihash).slice(0, 8)} → {String(a.ohash).slice(0, 8)}
        </span>
      ),
    },
  ];

  return (
    <div>
      <PageShell title={h.title} description={h.desc} pills={pills} />

      <div style={{ display: "grid", gridTemplateColumns: "repeat(4,1fr)", gap: 12, marginBottom: 16 }}>
        {stats.map((s) => (
          <StatTile key={s.label} label={s.label} value={s.value} color={s.color} />
        ))}
      </div>

      <div style={{ display: "flex", gap: 6, marginBottom: 16, flexWrap: "wrap", alignItems: "center" }}>
        {TABS.map((t) => (
          <Button key={t.v} variant={filter === t.v ? "primary" : "ghost"} size="sm" onClick={() => setFilter(t.v)}>
            {t.label}
          </Button>
        ))}
      </div>

      <Panel rail={rail} mnemonic="AUDIT" description="audit_log — safety callback chain" live>
        {empty ? (
          hasAnyFilter ? (
            <EmptyState
              kind="filter-zero"
              title="No audit entries match the current filter"
              hint="Try a different tab, or clear back to All."
              action={{ label: "Clear filter", onClick: () => setFilter("(all)") }}
            />
          ) : (
            <EmptyState
              kind="none-yet"
              title="No audit log entries"
              hint="Entries will appear here once agent tool calls are audited."
            />
          )
        ) : (
          <DataTable<AuditEntry>
            columns={columns}
            rows={filtered}
            rowKey={(a, i) => `${a.ts}-${i}`}
            sortable
            initialSortKey="ts"
            initialSortDir="desc"
            toolbarName="audit_log"
            caption="Audit log: timestamp, agent, tool, safety layer, verdict, reason, and I/O hashes"
            statusBar={{
              shown: filtered.length,
              total: noDataAtAll ? 0 : totalAudited,
              note: "read-only · no mutations possible from this view",
            }}
          />
        )}
      </Panel>
    </div>
  );
}

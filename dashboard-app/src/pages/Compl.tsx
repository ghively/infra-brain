import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate, type NavigateFunction } from "react-router-dom";
import { apiGet, rows, AuthRequired, redirectToLogin } from "../api";
import { DetailDrawer } from "../components/DetailDrawer";
import { Skeleton } from "../components/Skeleton";
import { headerFor } from "../lib/headers";
import { fmtDt } from "../lib/fmtDt";
import { useAutoRefresh } from "../hooks/useAutoRefresh";
import {
  PageShell,
  StatTile,
  Panel,
  DataTable,
  EmptyState,
  Tabs,
  SeverityPill,
  Badge,
  FkCell,
  type ColumnDef,
} from "../components/ui";
import { worstSeverity } from "../lib/tokens";

// Port of dashboard/src/pages/compl.dc.html + the complStats/complTabs/complView
// derivation in the legacy renderVals() (static/index.html:6427-6437), rebuilt
// on the Phase 1 design system (see dashboard-app/DESIGN.md) as a Phase 3
// DataTable conversion. Backed by GET /api/dashboard/compliance (CompliancePageOut
// envelope, src/infra_brain/api/routers/governance.py:518).
//
// STRUCTURE CALL (this rebuild): the legacy/pre-rebuild page rendered one
// stacked `.ib-card` per finding. Compliance findings are homogeneous rows
// sharing the same six fields (rule/severity/host/detail/status/detected_at),
// which per DESIGN.md §8 is the canonical DataTable case (comparing many
// records across the same fields) — converted to a `DataTable` here, same
// call as Eol.tsx's rebuild in this batch.
//
// SEVERITY CALL: compliance severities (critical/severe/high/moderate/low)
// already fall inside `normalizeSeverity`'s accepted aliases (including the
// Rapid7 "severe"→high mapping), so this rebuild renders severity via the
// shared `SeverityPill` instead of the page-local `sevStyle()` hex palette.
//
// STATUS CALL: `open`/`resolved` is a health axis, not a severity axis, so it
// renders via `Badge`. There is no compliance-specific entry in
// `normalizeStatus` (its "ok" list doesn't include "resolved"), so this page
// keeps a small local mapping (open→info, resolved→neutral) rather than
// widening the shared status vocabulary for one page's two values — same
// precedent as Vulns.tsx's `statusBadgeTone` for its "open" case.
//
// DEVIATION FROM LEGACY: the router's `status` query param defaults to "open"
// (governance.py:519). The legacy page called load('/compliance','COMPL') with
// no query params (static/index.html:3750), so it only ever had open violations
// in memory — meaning the "Resolved" tab and the "Resolved" stat
// (static/index.html:6437) were dead: filtering an all-open array by
// status==='resolved' is always []. This port passes status= (empty string,
// which the router's `if status:` treats as "no filter") to actually fetch
// every status, so the Resolved tab and stat are real.
//
// Row click opens a DetailDrawer (../components/DetailDrawer) with the full
// compliance-finding detail, matching the legacy detail:{kind:'compl'} drawer
// (static/index.html:3343-3391). The clicked ComplRow is passed straight into
// the page-local <ComplDetail/> — all fields are already in the fetched rows.
//
// ACCURACY NOTE (T5-compl audit fix; RESOLVED by TRK-167 follow-up): the
// list fetch below is still capped at limit=500 (governance_ops.py's
// list_compliance default), but the Open/Resolved stat tiles no longer
// derive from that capped `rowsData` — they now come from the fleet-wide
// `compliance_open`/`compliance_resolved` aggregate on GET
// /api/dashboard/counts (fleet.py::get_counts(), same pattern as
// critical_cves/severe_cves on Vulns.tsx), so they sum correctly with
// Total regardless of the page cap.

type ComplRow = {
  rule: string;
  severity: string;
  host: string;
  detail: string;
  status: string;
  detected_at: string;
};

const TABS: [string, string][] = [
  ["(all)", "All"],
  ["open", "Open"],
  ["resolved", "Resolved"],
];

/** Subset of CountsOut (src/infra_brain/api/schemas.py) this page needs —
 *  fleet-wide open/resolved ComplianceViolation counts (TRK-167 follow-up),
 *  same pattern as Vulns.tsx's `Counts` type. */
type Counts = { compliance_open: number; compliance_resolved: number };

/** `open`/`resolved` → Badge tone. See the "STATUS CALL" note above. */
function complStatusTone(status: string): "info" | "neutral" {
  return status === "open" ? "info" : "neutral";
}

/** Detail content for a single compliance finding, shown inside DetailDrawer.
 *  Port of the legacy detail:{kind:'compl'} drawer (static/index.html:3343-3391):
 *  severity + status pills, a Details block (Rule / Host / Detected), and a
 *  Finding block with the full detail text — restyled on the shared
 *  SeverityPill/Badge primitives and `--ib-*` tokens instead of hand-picked hex. */
function ComplDetail({ item, navigate }: { item: ComplRow; navigate: NavigateFunction }) {
  const kv = (label: string, value: React.ReactNode) => (
    <div style={{ display: "flex", justifyContent: "space-between", padding: "11px 14px", background: "var(--ib-raised)" }}>
      <span style={{ fontSize: 12, color: "var(--ib-muted)" }}>{label}</span>
      <span
        style={{
          fontSize: 12,
          color: "var(--ib-text)",
          fontFamily: "var(--ib-mono, 'DM Mono', monospace)",
          maxWidth: "60%",
          textAlign: "right",
          wordBreak: "break-all",
        }}
      >
        {value}
      </span>
    </div>
  );

  return (
    <div>
      <div style={{ display: "flex", gap: 8, marginBottom: 22, flexWrap: "wrap" }}>
        <SeverityPill severity={item.severity} />
        <Badge tone={complStatusTone(item.status)}>{item.status}</Badge>
      </div>

      <div style={{ fontSize: 10, fontWeight: 700, color: "var(--ib-faint)", textTransform: "uppercase", letterSpacing: ".1em", marginBottom: 12 }}>
        Details
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 1, background: "var(--ib-border)", borderRadius: "var(--ib-radius)", overflow: "hidden", marginBottom: 22 }}>
        {kv("Rule", item.rule)}
        {kv(
          "Host",
          // onClick+navigate, not a raw `href` — see the columns-array Host
          // cell below for the full F-1 rationale (basename escape + dead
          // `?host=` param); this drawer copy of the same cell had the
          // identical defect.
          <FkCell
            label={item.host}
            onClick={() => navigate(`/hosts?tab=hosts&q=${encodeURIComponent(item.host)}`)}
          />,
        )}
        {kv("Detected", <span style={{ color: "var(--ib-text)" }}>{fmtDt(item.detected_at)}</span>)}
      </div>

      <div style={{ fontSize: 10, fontWeight: 700, color: "var(--ib-faint)", textTransform: "uppercase", letterSpacing: ".1em", marginBottom: 10 }}>
        Finding
      </div>
      <div style={{ background: "var(--ib-raised)", border: "1px solid var(--ib-border)", borderRadius: "var(--ib-radius)", padding: "14px 16px" }}>
        <div style={{ fontSize: 13, color: "var(--ib-text)", lineHeight: 1.6, whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
          {item.detail}
        </div>
      </div>
    </div>
  );
}

export function Compl() {
  const navigate = useNavigate();
  const [rowsData, setRowsData] = useState<ComplRow[] | null>(null);
  // FE-6: true DB-wide violation count from the {items,total,...} envelope.
  const [totalRows, setTotalRows] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState("(all)");
  const [selected, setSelected] = useState<ComplRow | null>(null);
  // Fleet-wide open/resolved counts (TRK-167 follow-up) — independent of the
  // capped list fetch below, so it's loaded once on mount and refreshed on
  // the normal auto-refresh cycle, same as Vulns.tsx's `counts` state.
  const [counts, setCounts] = useState<Counts | null>(null);

  const load = useCallback(async () => {
    const d = await apiGet<unknown>("/api/dashboard/compliance?status=&limit=500");
    setRowsData(rows<ComplRow>(d));
    setTotalRows((d as { total?: number }).total ?? null);
    await apiGet<Counts>("/api/dashboard/counts")
      .then((c) => setCounts(c))
      .catch((e) => {
        if (e instanceof AuthRequired) redirectToLogin();
        // Otherwise leave counts as-is (null -> tiles fall back to 0) — a
        // failed counts fetch shouldn't blank out the rest of the page.
      });
  }, []);

  useEffect(() => {
    load().catch((e) => {
      if (e instanceof AuthRequired) redirectToLogin();
      else setError(String(e));
    });
  }, [load]);

  useAutoRefresh(load);

  const view = useMemo(() => {
    if (!rowsData) return [];
    return filter === "(all)" ? rowsData : rowsData.filter((c) => c.status === filter);
  }, [rowsData, filter]);

  const h = headerFor("compl");

  if (error) {
    return (
      <div>
        <PageShell title={h.title} description={h.desc} />
        <EmptyState kind="error" title="Compliance failed to load" hint={error} />
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

  // Open/Resolved come from the fleet-wide compliance_open/compliance_resolved
  // aggregate on GET /api/dashboard/counts (TRK-167 follow-up) — real DB-wide
  // totals, not counted from the capped `rowsData` fetch, so they sum
  // correctly with Total regardless of the list endpoint's limit=500 cap.
  const stats: Array<{ label: string; value: string; color: "red" | "yellow" | "green" | "blue" | undefined }> = [
    { label: "Open", value: String(counts?.compliance_open ?? 0), color: "red" },
    { label: "Resolved", value: String(counts?.compliance_resolved ?? 0), color: "green" },
    { label: "Total", value: String(totalRows ?? rowsData.length), color: undefined },
  ];

  const rail = worstSeverity(view.map((c) => c.severity));

  const columns: ColumnDef<ComplRow>[] = [
    { key: "rule", header: "Rule", typeGlyph: "pk" },
    {
      key: "host",
      header: "Host",
      typeGlyph: "fk",
      // Cross-page navigation to the unified Hosts view, filtered to this
      // host. `?host=` is read by nothing (Hosts.tsx only reads `?tab=`,
      // UnifiedHostsTab.tsx only reads `?q=`) and a raw `href` escapes the
      // router's `basename="/dashboard2"` into a hard-navigation 404 against
      // the backend (F-1) — onClick+navigate with `?tab=hosts&q=...` fixes
      // both, matching Vulns.tsx/Eol.tsx/Invrec.tsx's identical drill-down.
      render: (c) => (
        <FkCell label={c.host} onClick={() => navigate(`/hosts?tab=hosts&q=${encodeURIComponent(c.host)}`)} />
      ),
    },
    { key: "detail", header: "Detail", typeGlyph: "text" },
    {
      key: "severity",
      header: "Severity",
      typeGlyph: "enum",
      render: (c) => <SeverityPill severity={c.severity} />,
    },
    {
      key: "status",
      header: "Status",
      typeGlyph: "enum",
      render: (c) => <Badge tone={complStatusTone(c.status)}>{c.status}</Badge>,
    },
    { key: "detected_at", header: "Detected", typeGlyph: "text", render: (c) => fmtDt(c.detected_at) },
  ];

  return (
    <div>
      <PageShell title={h.title} description={h.desc} />
      <div style={{ display: "grid", gridTemplateColumns: "repeat(3,1fr)", gap: 12, marginBottom: 16 }}>
        {stats.map((s) => (
          <StatTile key={s.label} label={s.label} value={s.value} color={s.color} />
        ))}
      </div>

      <div style={{ marginBottom: 16 }}>
        <Tabs
          label="Compliance status filter"
          tabs={TABS.map(([v, label]) => ({ key: v, label }))}
          active={filter}
          onChange={setFilter}
        />
      </div>

      <Panel
        rail={rail === "critical" || rail === "high" ? "err" : rail === "medium" ? "warn" : rail ? "ok" : "info"}
        mnemonic="COMPL"
        description="compliance_violations"
        live
      >
        {view.length === 0 ? (
          rowsData.length === 0 ? (
            // Nothing at ANY status — ComplianceAgent (agents/compliance.py)
            // hasn't produced a finding yet in this environment. It runs
            // deterministic rules (eol_overdue, vuln_sla_breach, stale_drift,
            // unaddressed_critical_cve) over already-collected inventory, so
            // this is "hasn't run yet", not "not configured" — there's no
            // credential this agent needs.
            <EmptyState
              kind="none-yet"
              title="No compliance findings recorded yet"
              hint="ComplianceAgent evaluates EOL/vuln-SLA/drift rules against already-collected inventory on its own schedule — see Scan Schedule for when it next runs."
            />
          ) : filter === "open" ? (
            // Findings exist elsewhere (resolved) but zero are currently
            // open — this is the good-news case, not a gap.
            <EmptyState
              kind="clean"
              title="No open compliance violations"
              hint="Every EOL/vuln-SLA/drift rule currently passes. This is the healthy state, not missing data."
            />
          ) : (
            <EmptyState
              kind="filter-zero"
              title="No compliance findings in this status"
              hint="Switch tabs to see findings in a different status."
            />
          )
        ) : (
          <DataTable<ComplRow>
            columns={columns}
            rows={view}
            rowKey={(c, i) => `${c.rule}-${c.host}-${i}`}
            sortable
            onRowClick={(c) => setSelected(c)}
            toolbarName="compliance_violations"
            caption="Compliance violations: rule, host, detail, severity, status, and detection time"
            statusBar={{ shown: view.length, total: totalRows ?? rowsData.length }}
          />
        )}
      </Panel>

      <DetailDrawer title={selected ? selected.rule : "Compliance finding"} open={selected !== null} onClose={() => setSelected(null)}>
        {selected && <ComplDetail item={selected} navigate={navigate} />}
      </DetailDrawer>
    </div>
  );
}

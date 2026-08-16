import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { apiGet, rows, AuthRequired, redirectToLogin } from "../api";
import { DetailDrawer } from "../components/DetailDrawer";
import { headerFor } from "../lib/headers";
import { useAutoRefresh } from "../hooks/useAutoRefresh";
import {
  PageShell,
  StatTile,
  Panel,
  DataTable,
  Tabs,
  EmptyState,
  Badge,
  FkCell,
  NullCell,
  DomainCell,
  type ColumnDef,
  type StatColor,
} from "../components/ui";
import { statusColor } from "../lib/tokens";

type ReconcileEvent = {
  host: string;
  domain: string;
  target_group: string;
  status: string;
  mr_url: string;
  detected_at: string;
};

const TABS = [
  { key: "(all)", label: "All" },
  { key: "proposed", label: "Proposed" },
  { key: "merged", label: "Merged" },
  { key: "rejected", label: "Rejected" },
];

const STATUS_TONE: Record<string, "info" | "ok" | "err"> = {
  proposed: "info",
  merged: "ok",
  rejected: "err",
};

const STATUS_STAT_COLOR: Record<string, StatColor> = {
  proposed: "blue",
  merged: "green",
  rejected: "red",
};

function statusTone(status: string): "info" | "ok" | "err" | "neutral" {
  return STATUS_TONE[status] ?? "neutral";
}

function statusStatColor(status: string): StatColor {
  return STATUS_STAT_COLOR[status] ?? undefined;
}

function fmtDt(s: string | undefined): string {
  if (!s) return "—";
  const d = String(s);
  return d.length > 19 ? `${d.slice(0, 10)} ${d.slice(11, 19)}` : d;
}

/** Detail content for a single reconcile event, shown inside DetailDrawer.
 *  Ports the legacy `detailIsInvrec` panel: a Proposal field list (Host,
 *  Domain, Target group, Detected) plus a "View merge request →" link when
 *  mr_url is present. `hasMr = x.mr_url && x.mr_url !== '—'` mirrored here so
 *  an absent/placeholder URL renders no link (no dead link). This page has no
 *  approve/reject action of its own — inventory reconciliation has no
 *  corresponding endpoint (governance.py's inventory-reconcile routes are
 *  list-only); the MR link is the only actionable affordance, same as before
 *  the rebuild. */
function ReconcileDetail({ item }: { item: ReconcileEvent }) {
  const hasMr = !!item.mr_url && item.mr_url !== "—";

  const row = (label: string, value: React.ReactNode) => (
    <div style={{ display: "flex", gap: 8, marginBottom: 10 }}>
      <span style={{ fontSize: 11, color: "var(--ib-muted)", fontWeight: 600, minWidth: 110, flexShrink: 0 }}>
        {label}
      </span>
      <span style={{ fontSize: 12, color: "var(--ib-text)", fontFamily: "var(--ib-mono, 'DM Mono', monospace)", wordBreak: "break-all" }}>
        {value}
      </span>
    </div>
  );

  return (
    <div style={{ padding: "4px 0" }}>
      {row("Status", <Badge tone={statusTone(item.status)}>{item.status}</Badge>)}
      {row("Host", item.host)}
      {row("Domain", item.domain)}
      {row("Target group", item.target_group)}
      {row("Detected", fmtDt(item.detected_at))}
      {hasMr && (
        <a
          href={item.mr_url}
          target="_blank"
          rel="noopener noreferrer"
          style={{
            display: "block",
            textAlign: "center",
            fontSize: 12,
            fontWeight: 600,
            color: "var(--ib-blue)",
            background: "rgba(91,157,232,.08)",
            border: "1px solid rgba(91,157,232,.22)",
            borderRadius: 9,
            padding: "10px 14px",
            textDecoration: "none",
            marginTop: 14,
          }}
        >
          View merge request &rarr;
        </a>
      )}
    </div>
  );
}

/** Subset of CountsOut (src/infra_brain/api/schemas.py) this page needs.
 *  fleet.py's CountsOut exposes real DB-wide invrec_proposed / invrec_total
 *  aggregates — unlike the page's own capped (limit=500) fetch, these are
 *  plain COUNT queries unaffected by the cap. There's no merged/rejected
 *  equivalent, so those two stay page-scoped and are labeled honestly (same
 *  "(page)" convention as Compl.tsx / Remed.tsx). */
type Counts = { invrec_proposed: number; invrec_total: number };

/** Port of invrec.dc.html (legacy) — inventory reconciliation proposal list,
 *  backed by GET /api/dashboard/inventory_reconcile
 *  (src/infra_brain/api/routers/governance.py:491), returning
 *  InventoryReconcilePageOut {items,total,limit,offset}. Legacy loaded once
 *  and filtered/tabbed client-side; ported the same way.
 *
 *  Rebuilt on the Phase 1 design system (see dashboard-app/DESIGN.md):
 *  PageShell/StatTile/Panel/DataTable/Badge instead of hand-stamped
 *  inline-styled divs. Row click opens a DetailDrawer with the reconcile-event
 *  detail, including detected_at and a clickable mr_url link.
 *
 *  Fix (this rebuild): the table row previously rendered `mr_url` as dead
 *  monospace text while the drawer rendered it as a live link — the same
 *  value read two different ways depending on where you looked. The table
 *  row now uses `FkCell`/`NullCell` so an MR link is a real link (or an
 *  explicit NULL) in both places. */
export function Invrec() {
  const navigate = useNavigate();
  const [all, setAll] = useState<ReconcileEvent[] | null>(null);
  // FE-6: true DB-wide event count from the {items,total,...} envelope.
  const [totalEvents, setTotalEvents] = useState<number | null>(null);
  const [counts, setCounts] = useState<Counts | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState("(all)");
  const [selected, setSelected] = useState<ReconcileEvent | null>(null);
  const triggerRef = useRef<HTMLElement>(null);

  const load = useCallback(async () => {
    const d = await apiGet<unknown>("/api/dashboard/inventory_reconcile?limit=500");
    setAll(rows<ReconcileEvent>(d));
    setTotalEvents((d as { total?: number }).total ?? null);
    await apiGet<Counts>("/api/dashboard/counts")
      .then((c) => setCounts(c))
      .catch((e) => {
        if (e instanceof AuthRequired) redirectToLogin();
        // Otherwise leave counts as-is — a failed counts fetch shouldn't
        // block the rest of the page from loading.
      });
  }, []);

  useEffect(() => {
    load().catch((e) => {
      if (e instanceof AuthRequired) redirectToLogin();
      else setError(String(e));
    });
  }, [load]);

  useAutoRefresh(load);

  const h = headerFor("invrec");

  if (error) {
    return (
      <div>
        <PageShell title={h.title} description={h.desc} />
        <EmptyState kind="error" title="Inventory reconciliation failed to load" hint={error} />
      </div>
    );
  }

  if (all === null) {
    return (
      <div>
        <PageShell title={h.title} description={h.desc} />
        <Panel mnemonic="INVREC" description="inventory_reconcile" rail="info">
          <div style={{ padding: 16 }}>Loading…</div>
        </Panel>
      </div>
    );
  }

  const proposedCount = all.filter((x) => x.status === "proposed").length;
  const mergedCount = all.filter((x) => x.status === "merged").length;
  const rejectedCount = all.filter((x) => x.status === "rejected").length;

  // Tile colors are pinned to the same status→tone mapping as the row
  // badges so a tile and its matching row badge are always visually
  // consistent. Rejected is the one negative outcome and previously had a
  // tab but no stat tile — added here so all three real outcomes
  // (proposed/merged/rejected) are visible in the header, not just the two
  // positive-path ones.
  const stats: Array<{ label: string; value: number; status: string }> = [
    { label: "Proposed", value: counts?.invrec_proposed ?? proposedCount, status: "proposed" },
    { label: "Merged (page)", value: mergedCount, status: "merged" },
    { label: "Rejected (page)", value: rejectedCount, status: "rejected" },
    { label: "Total", value: counts?.invrec_total ?? totalEvents ?? all.length, status: "" },
  ];

  const filtered = filter === "(all)" ? all : all.filter((x) => x.status === filter);

  const pills = [
    { dot: statusColor("info"), text: `${counts?.invrec_proposed ?? proposedCount} proposed` },
    { dot: statusColor("ok"), text: `${mergedCount} merged` },
    { dot: statusColor("error"), text: `${rejectedCount} rejected` },
  ];

  const hasFilter = filter !== "(all)";
  const empty = filtered.length === 0;

  const columns: ColumnDef<ReconcileEvent>[] = [
    {
      key: "host",
      header: "Host",
      typeGlyph: "fk",
      // Cross-page navigation to the unified Hosts view, filtered to this
      // host. FkCell only supports either a plain <a href> (hard navigation,
      // wrong for an in-SPA route under BrowserRouter basename="/dashboard2")
      // or an onClick — so this follows the onClick+navigate() pattern
      // already established by ResourcesTab.tsx's hostname FkCell, landing
      // on UnifiedHostsTab's `?q=` search param (which it already reads and
      // forwards to GET /api/dashboard/hosts?q=... as a short_hostname/fqdn
      // substring filter — see hosts.py's list_hosts).
      render: (x) => (
        <FkCell
          label={x.host}
          title={`View ${x.host} in the unified Hosts view`}
          onClick={(e) => {
            e.stopPropagation();
            navigate(`/hosts?tab=hosts&q=${encodeURIComponent(x.host)}`);
          }}
        />
      ),
    },
    {
      key: "domain",
      header: "Domain",
      typeGlyph: "enum",
      render: (x) => <DomainCell name={x.domain} />,
    },
    { key: "target_group", header: "Target group", typeGlyph: "text" },
    {
      key: "mr_url",
      header: "MR",
      typeGlyph: "fk",
      sortable: false,
      render: (x) =>
        x.mr_url && x.mr_url !== "—" ? (
          <FkCell label="View MR" href={x.mr_url} title={x.mr_url} />
        ) : (
          <NullCell />
        ),
    },
    {
      key: "status",
      header: "Status",
      typeGlyph: "enum",
      render: (x) => <Badge tone={statusTone(x.status)}>{x.status}</Badge>,
    },
    {
      key: "detected_at",
      header: "Detected",
      typeGlyph: "text",
      render: (x) => fmtDt(x.detected_at),
    },
  ];

  return (
    <div>
      <PageShell icon="🔄" title={h.title} description={h.desc} pills={pills} />

      <div style={{ display: "grid", gridTemplateColumns: "repeat(4,1fr)", gap: 12, marginBottom: 16 }}>
        {stats.map((s) => (
          <StatTile key={s.label} label={s.label} value={s.value} color={s.status ? statusStatColor(s.status) : undefined} />
        ))}
      </div>

      <div style={{ marginBottom: 16 }}>
        <Tabs tabs={TABS} active={filter} onChange={setFilter} label="Inventory reconciliation status" />
      </div>

      <Panel mnemonic="INVREC" description="inventory_reconcile" rail="info" live>
        {empty ? (
          hasFilter ? (
            <EmptyState
              kind="filter-zero"
              title="No reconciliation proposals in this status"
              hint="Switch tabs to see other proposals."
              action={{ label: "Clear filter", onClick: () => setFilter("(all)") }}
            />
          ) : (
            <EmptyState
              kind="none-yet"
              title="No reconciliation proposals yet"
              hint="Proposals will appear here after the next inventory sweep."
            />
          )
        ) : (
          <DataTable<ReconcileEvent>
            columns={columns}
            rows={filtered}
            rowKey={(x, i) => `${x.host}-${x.domain}-${i}`}
            sortable
            toolbarName="inventory_reconcile"
            caption="Inventory reconciliation proposals: host, domain, target group, MR, status, and detection time"
            onRowClick={(x) => setSelected(x)}
            statusBar={{ shown: filtered.length, total: totalEvents ?? undefined, note: "read-only · no mutations possible from this view" }}
          />
        )}
      </Panel>

      <DetailDrawer
        title={selected ? `${selected.host} — ${selected.domain}` : "Reconcile detail"}
        open={selected !== null}
        onClose={() => setSelected(null)}
        triggerRef={triggerRef}
      >
        {selected && <ReconcileDetail item={selected} />}
      </DetailDrawer>
    </div>
  );
}

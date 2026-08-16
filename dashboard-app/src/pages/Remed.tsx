import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { apiGet, apiPost, rows, AuthRequired, redirectToLogin } from "../api";
import { DetailDrawer } from "../components/DetailDrawer";
import { Skeleton } from "../components/Skeleton";
import { headerFor } from "../lib/headers";
import { useAutoRefresh } from "../hooks/useAutoRefresh";
import { RemedRollup } from "./RemedRollup";
import {
  PageShell,
  StatTile,
  Panel,
  DataTable,
  Tabs,
  Button,
  EmptyState,
  Badge,
  FkCell,
  NullCell,
  type ColumnDef,
  type StatColor,
} from "../components/ui";
import { statusColor } from "../lib/tokens";
import { fmtDt } from "../lib/fmtDt";

type Remediation = {
  id: string;
  agent: string;
  action_type: string;
  target: string;
  confidence: number;
  status: string;
  approved_by: string;
  result_url: string;
  created_at: string;
};

// approved and executed previously shared an identical green badge, making
// two distinct real-world states (human approved it vs. the agent actually
// executed it) visually indistinguishable. executed keeps the "ok/done" tone;
// approved gets the "warn/in-between" tone — this mirrors the pre-rebuild
// STATUS_STYLE distinction, now expressed through the shared Badge tone set
// instead of hand-picked hex.
const STATUS_TONE: Record<string, "info" | "warn" | "ok" | "err"> = {
  pending: "info",
  approved: "warn",
  executed: "ok",
  rejected: "err",
};

const STATUS_STAT_COLOR: Record<string, StatColor> = {
  pending: "blue",
  approved: "yellow",
  executed: "green",
  rejected: "red",
};

function statusTone(status: string): "info" | "warn" | "ok" | "err" {
  return STATUS_TONE[status] ?? "info";
}

function statusStatColor(status: string): StatColor {
  return STATUS_STAT_COLOR[status] ?? undefined;
}

// Per-action-type pill tint (restores legacy per-type colouring — a prior
// pass had collapsed every action_type pill to one hardcoded indigo). Not a
// severity or a status — just a categorical label — so it's rendered as a
// plain Badge with tone cycling through the four hues rather than inventing
// a fifth color family.
const ACTION_TYPE_TONE: Record<string, "info" | "ok" | "warn" | "err"> = {
  restart_service: "info",
  patch: "ok",
  scale: "warn",
  rollback: "err",
};

function actionTypeTone(actionType: string): "info" | "ok" | "warn" | "err" | "neutral" {
  return ACTION_TYPE_TONE[actionType] ?? "neutral";
}

const TABS = [
  { key: "(all)", label: "All" },
  { key: "pending", label: "Pending" },
  { key: "approved", label: "Approved" },
  { key: "executed", label: "Executed" },
  { key: "rejected", label: "Rejected" },
];

// Task 14 / TRK-255: Detail (flat list, existing) vs Rollup (dedup/aggregate
// over pending ProposedAction rows, grouped by action_type + field).
const VIEW_MODE_TABS = [
  { key: "detail", label: "Detail" },
  { key: "rollup", label: "Rollup" },
];

/** Detail panel for a single remediation action, shown inside the
 *  DetailDrawer on row click. Approve/Reject buttons POST to the actions API
 *  and update parent state on success — same interaction shape as
 *  `Intprops.tsx`'s `IntpropDetail` (itself copied from this component before
 *  this rebuild), now on the Phase 1 primitives.
 *
 *  Endpoints:
 *    POST /api/dashboard/actions/{id}/approve  (body: {})
 *    POST /api/dashboard/actions/{id}/reject   (no body)
 */
function RemedDetail({
  item,
  onStatusChange,
}: {
  item: Remediation;
  onStatusChange: (id: string, newStatus: string) => void;
}) {
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirmingReject, setConfirmingReject] = useState(false);

  const confNum =
    typeof item.confidence === "number" ? item.confidence : parseFloat(String(item.confidence || "0")) || 0;

  const isPending = item.status === "pending";
  const canApprove = isPending && confNum >= 0.7;
  const canReject = isPending;
  const lowConf = isPending && confNum < 0.7;
  const hasMr = !!item.result_url && item.result_url !== "—";

  async function handleApprove() {
    setBusy(true);
    setMsg("Approving…");
    try {
      await apiPost(`/api/dashboard/actions/${encodeURIComponent(item.id)}/approve`, {});
      onStatusChange(item.id, "approved");
      setMsg("Approved. The proposing agent will execute it on its next run.");
    } catch (e) {
      if (e instanceof AuthRequired) {
        redirectToLogin();
        return;
      }
      setMsg(`Approve failed: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(false);
    }
  }

  async function handleReject() {
    if (!confirmingReject) {
      setConfirmingReject(true);
      return;
    }
    setBusy(true);
    setMsg("Rejecting…");
    try {
      await apiPost(`/api/dashboard/actions/${encodeURIComponent(item.id)}/reject`);
      onStatusChange(item.id, "rejected");
      setMsg("Rejected.");
    } catch (e) {
      if (e instanceof AuthRequired) {
        redirectToLogin();
        return;
      }
      setMsg(`Reject failed: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(false);
      setConfirmingReject(false);
    }
  }

  const row = (label: string, value: React.ReactNode) => (
    <div style={{ display: "flex", gap: 8, marginBottom: 10 }}>
      <span style={{ fontSize: 11, color: "var(--ib-muted)", fontWeight: 600, minWidth: 100, flexShrink: 0 }}>
        {label}
      </span>
      <span style={{ fontSize: 12, color: "var(--ib-text)", fontFamily: "var(--ib-mono, 'DM Mono', monospace)", wordBreak: "break-all" }}>
        {value}
      </span>
    </div>
  );

  return (
    <div style={{ padding: "4px 0" }}>
      <div style={{ marginBottom: 16 }}>
        <Badge tone={actionTypeTone(item.action_type)}>{item.action_type}</Badge>
      </div>

      {row("Target", item.target)}
      {row("Agent", item.agent)}
      {row(
        "Confidence",
        <span style={{ color: confNum >= 0.7 ? statusColor("ok") : statusColor("warn") }}>
          {confNum.toFixed(2)} ({Math.round(confNum * 100)}%)
        </span>,
      )}
      {row("Status", <Badge tone={statusTone(item.status)}>{item.status}</Badge>)}
      {item.approved_by && item.approved_by !== "—" && row("Approved by", item.approved_by)}
      {hasMr &&
        row(
          "MR / Result",
          <a href={item.result_url} target="_blank" rel="noopener noreferrer" style={{ color: "var(--ib-blue)" }}>
            {item.result_url}
          </a>,
        )}
      {item.created_at && row("Created", fmtDt(item.created_at))}

      {lowConf && (
        <div
          style={{
            fontSize: 11,
            color: statusColor("warn"),
            background: "rgba(217,166,46,.08)",
            border: "1px solid rgba(217,166,46,.2)",
            borderRadius: 7,
            padding: "8px 12px",
            marginBottom: 14,
          }}
        >
          Confidence below 0.70 — cannot approve until confidence is raised.
        </div>
      )}

      {isPending && (
        <div style={{ display: "flex", gap: 8, marginTop: 16, alignItems: "center" }}>
          {canApprove && (
            <Button variant="primary" size="md" onClick={handleApprove} loading={busy}>
              Approve
            </Button>
          )}
          {canReject && (
            <Button variant={confirmingReject ? "danger" : "secondary"} size="md" onClick={handleReject} loading={busy}>
              {confirmingReject ? "Click again to confirm" : "Reject"}
            </Button>
          )}
          {confirmingReject && !busy && (
            <Button variant="ghost" size="md" onClick={() => setConfirmingReject(false)}>
              Cancel
            </Button>
          )}
        </div>
      )}

      {msg && (
        <div
          style={{
            marginTop: 12,
            fontSize: 12,
            color: msg.toLowerCase().includes("failed") ? statusColor("error") : statusColor("ok"),
            padding: "8px 12px",
            borderRadius: 7,
            background: msg.toLowerCase().includes("failed") ? "rgba(240,101,78,.08)" : "rgba(76,187,108,.08)",
            border: msg.toLowerCase().includes("failed")
              ? "1px solid rgba(240,101,78,.2)"
              : "1px solid rgba(76,187,108,.2)",
          }}
        >
          {msg}
        </div>
      )}
    </div>
  );
}

/** Port of legacy remed.dc.html (Remediation page). Backed by GET
 *  /api/dashboard/remediation (vuln.py:130) — RemediationPageOut
 *  {items,total,limit,offset}, all loaded once; the status tabs filter
 *  client-side exactly like the legacy `remedTabs`/`remedView` derivation.
 *
 *  Rebuilt on the Phase 1 design system (see dashboard-app/DESIGN.md):
 *  PageShell/StatTile/Panel/DataTable/Badge instead of hand-stamped
 *  inline-styled divs. Row click opens a DetailDrawer with full remediation
 *  detail and Approve/Reject action buttons — the same real state-changing
 *  operations as before, now sharing the shared `Button` primitive and the
 *  same interaction shape ported to `Intprops.tsx` in Phase 0. Actions POST to:
 *    /api/dashboard/actions/{id}/approve  (body: {})
 *    /api/dashboard/actions/{id}/reject   (no body)
 *  and optimistically update the in-memory item status on success. */
export function Remed() {
  const [remed, setRemed] = useState<Remediation[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState("(all)");
  const [selected, setSelected] = useState<Remediation | null>(null);
  const triggerRef = useRef<HTMLDivElement>(null);
  // Task 14 / TRK-255: dedup-rollup aggregate alongside the existing flat
  // detail list. actionTypeFilter is set when a rollup group row is clicked
  // ("drill through") — RemediationOut carries no `field` column (only the
  // ProposedAction.payload the rollup endpoint reads server-side does), so
  // action_type is the closest filter available on the detail page.
  const [viewMode, setViewMode] = useState<"detail" | "rollup">("detail");
  const [actionTypeFilter, setActionTypeFilter] = useState<string | null>(null);

  const load = useCallback(async () => {
    const d = await apiGet<unknown>("/api/dashboard/remediation?limit=500");
    setRemed(rows<Remediation>(d));
  }, []);

  useEffect(() => {
    load().catch((e) => {
      if (e instanceof AuthRequired) redirectToLogin();
      else setError(String(e));
    });
  }, [load]);

  useAutoRefresh(load);

  /** Called by RemedDetail when an approve/reject POST succeeds — update
   *  the in-memory list so stats and the open drawer both reflect the change. */
  function handleStatusChange(id: string, newStatus: string) {
    setRemed((prev) => (prev ? prev.map((r) => (r.id === id ? { ...r, status: newStatus } : r)) : prev));
    setSelected((prev) => (prev && prev.id === id ? { ...prev, status: newStatus } : prev));
  }

  const view = useMemo(() => {
    if (!remed) return [];
    let out = filter === "(all)" ? remed : remed.filter((r) => r.status === filter);
    if (actionTypeFilter) out = out.filter((r) => r.action_type === actionTypeFilter);
    return out;
  }, [remed, filter, actionTypeFilter]);

  const h = headerFor("remed");

  if (error) {
    return (
      <div>
        <PageShell title={h.title} description={h.desc} />
        <EmptyState kind="error" title="Remediation failed to load" hint={error} />
      </div>
    );
  }

  if (remed === null) {
    return (
      <div>
        <PageShell title={h.title} description={h.desc} />
        <Panel mnemonic="REMED" description="remediation_actions" rail="info">
          <div style={{ padding: 16 }}>
            <Skeleton count={3} height={32} />
          </div>
        </Panel>
      </div>
    );
  }

  // ACCURACY (T2 audit, preserved from the pre-rebuild page): these counts
  // are computed from `remed`, which is a capped fetch (limit=500 — see
  // load() above). Past 500 rows they silently undercount with no signal.
  // fleet.py's CountsOut has no remediation aggregate (open_drift/open_cves/
  // eol_*/invrec_*/total_resources/applied_instincts only — verified against
  // api/schemas.py::CountsOut), so there's nothing to source a true
  // per-status total from. Labeled "(page)" rather than left silently wrong;
  // a real fix needs a backend aggregate — follow-up.
  const pendingCount = remed.filter((x) => x.status === "pending").length;
  const approvedCount = remed.filter((x) => x.status === "approved").length;
  const executedCount = remed.filter((x) => x.status === "executed").length;
  const rejectedCount = remed.filter((x) => x.status === "rejected").length;

  const stats: Array<{ label: string; value: number; status: string }> = [
    { label: "Pending (page)", value: pendingCount, status: "pending" },
    { label: "Approved (page)", value: approvedCount, status: "approved" },
    { label: "Executed (page)", value: executedCount, status: "executed" },
    { label: "Rejected (page)", value: rejectedCount, status: "rejected" },
  ];

  const pills = [
    { dot: statusColor("info"), text: `${pendingCount} pending` },
    { dot: statusColor("warn"), text: `${approvedCount} approved` },
    { dot: statusColor("ok"), text: `${executedCount} executed` },
  ];

  const hasFilter = filter !== "(all)";
  const empty = view.length === 0;

  const columns: ColumnDef<Remediation>[] = [
    {
      key: "action_type",
      header: "Action",
      typeGlyph: "enum",
      render: (r) => <Badge tone={actionTypeTone(r.action_type)}>{r.action_type}</Badge>,
    },
    { key: "target", header: "Target", typeGlyph: "pk" },
    { key: "agent", header: "Agent", typeGlyph: "text" },
    {
      key: "confidence",
      header: "Confidence",
      typeGlyph: "num",
      accessor: (r) => r.confidence,
      render: (r) => {
        const c = typeof r.confidence === "number" ? r.confidence : parseFloat(String(r.confidence || "0")) || 0;
        return <span style={{ color: c >= 0.7 ? statusColor("ok") : statusColor("warn") }}>{c.toFixed(2)}</span>;
      },
    },
    {
      key: "result_url",
      header: "MR / Result",
      typeGlyph: "fk",
      render: (r) =>
        r.result_url && r.result_url !== "—" ? (
          <FkCell label="View" href={r.result_url} title={r.result_url} />
        ) : (
          <NullCell />
        ),
    },
    {
      key: "status",
      header: "Status",
      typeGlyph: "enum",
      render: (r) => <Badge tone={statusTone(r.status)}>{r.status}</Badge>,
    },
    {
      key: "created_at",
      header: "Created",
      typeGlyph: "text",
      render: (r) => fmtDt(r.created_at) ?? "—",
    },
  ];

  return (
    <div>
      <PageShell icon="🔧" title={h.title} description={h.desc} pills={pills} />

      <div style={{ display: "grid", gridTemplateColumns: "repeat(4,1fr)", gap: 12, marginBottom: 16 }}>
        {stats.map((s) => (
          <StatTile key={s.label} label={s.label} value={s.value} color={statusStatColor(s.status)} />
        ))}
      </div>

      <div style={{ marginBottom: 16 }}>
        <Tabs
          tabs={VIEW_MODE_TABS}
          active={viewMode}
          onChange={(v) => setViewMode(v as "detail" | "rollup")}
          label="Remediation view mode"
        />
      </div>

      {viewMode === "rollup" ? (
        <Panel mnemonic="REMED-ROLLUP" description="remediation_actions_rollup" rail="info">
          <RemedRollup
            onDrillToActionType={(actionType) => {
              setActionTypeFilter(actionType);
              setViewMode("detail");
            }}
          />
        </Panel>
      ) : (
        <>
          <div style={{ marginBottom: 16 }}>
            <Tabs tabs={TABS} active={filter} onChange={setFilter} label="Remediation status" />
          </div>

          {actionTypeFilter && (
            <div style={{ marginBottom: 12 }}>
              <Badge tone="info">
                filtered to action_type = {actionTypeFilter}
              </Badge>{" "}
              <Button variant="ghost" size="sm" onClick={() => setActionTypeFilter(null)}>
                Clear
              </Button>
            </div>
          )}

          <Panel mnemonic="REMED" description="remediation_actions" rail="info" live>
            {empty ? (
              hasFilter || actionTypeFilter ? (
                <EmptyState
                  kind="filter-zero"
                  title="No remediation actions in this status"
                  hint="Switch tabs to see other remediation actions."
                  action={{
                    label: "Clear filter",
                    onClick: () => {
                      setFilter("(all)");
                      setActionTypeFilter(null);
                    },
                  }}
                />
              ) : (
                <EmptyState
                  kind="none-yet"
                  title="No remediation actions yet"
                  hint="Actions will appear here after the next remediation sweep."
                />
              )
            ) : (
              <DataTable<Remediation>
                columns={columns}
                rows={view}
                rowKey={(r) => r.id}
                sortable
                toolbarName="remediation_actions"
                caption="Remediation actions: action type, target, agent, confidence, result, and status"
                onRowClick={(r) => setSelected(r)}
                statusBar={{ shown: view.length, note: "read-only · no mutations possible from this view" }}
              />
            )}
          </Panel>
        </>
      )}

      <DetailDrawer
        title={selected ? `${selected.action_type} — ${selected.target}` : "Remediation detail"}
        open={selected !== null}
        onClose={() => setSelected(null)}
        triggerRef={triggerRef as React.RefObject<HTMLElement>}
      >
        {selected && <RemedDetail item={selected} onStatusChange={handleStatusChange} />}
      </DetailDrawer>
    </div>
  );
}

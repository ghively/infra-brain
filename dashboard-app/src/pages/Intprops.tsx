import { useCallback, useEffect, useRef, useState } from "react";
import { apiGet, apiPost, rows, AuthRequired, redirectToLogin } from "../api";
import { DetailDrawer } from "../components/DetailDrawer";
import { Skeleton } from "../components/Skeleton";
import { Icon } from "../lib/icons";
import { headerFor } from "../lib/headers";
import { useAutoRefresh } from "../hooks/useAutoRefresh";
import {
  PageShell,
  StatTile,
  Panel,
  DataTable,
  Tabs,
  Button,
  EmptyState,
  Badge,
  type ColumnDef,
  type StatColor,
} from "../components/ui";
import { statusColor } from "../lib/tokens";
import { fmtDt } from "../lib/fmtDt";

type Proposal = {
  id: string;
  type: string;
  endpoint: string;
  confidence: number;
  proposed_at: string;
  status: string;
  source: string;
};

const TABS = [
  { key: "pending", label: "Pending" },
  { key: "approved", label: "Approved" },
  { key: "wired", label: "Wired" },
  { key: "rejected", label: "Rejected" },
  { key: "(all)", label: "All" },
];

// "wired" is intprops' terminal-success state (Remed.tsx's analogous field is
// "executed") — mirrors that page's tone table exactly: pending=info,
// approved=warn (in-between — a human said yes but nothing ran yet),
// wired=ok (done), rejected=err.
const STATUS_TONE: Record<string, "info" | "warn" | "ok" | "err"> = {
  pending: "info",
  approved: "warn",
  wired: "ok",
  rejected: "err",
};

const STATUS_STAT_COLOR: Record<string, StatColor> = {
  pending: "blue",
  approved: "yellow",
  wired: "green",
  rejected: "red",
};

function statusTone(status: string): "info" | "warn" | "ok" | "err" {
  return STATUS_TONE[status] ?? "info";
}

function statusStatColor(status: string): StatColor {
  return STATUS_STAT_COLOR[status] ?? undefined;
}

// TRK-182 follow-up: was hardcoded 0.7 here and in fleet.py's
// applied_instincts count — both now read the same Settings field
// (INTEGRATION_CONFIDENCE_GATE / integration_confidence_gate), fetched at
// runtime so the two surfaces can't drift apart. This fallback only applies
// before that fetch resolves.
//
// TRK-321: that fetch reads GET /api/dashboard/settings/ui, NOT
// /api/dashboard/settings. The latter is the full Settings model_dump() and is
// now admin-only; this page fetches the gate inside a Promise.all, so a 403
// there would reject the whole batch and blank the entire Integrations page —
// proposals included — for every non-admin user. /settings/ui is the narrow
// non-sensitive allowlist (see governance_ops._UI_SETTINGS_ALLOWLIST) that any
// signed-in session may read. Do not point this back at /settings.
const DEFAULT_CONFIDENCE_GATE = 0.7;

/** Confidence → color, 3 tiers exactly like the pre-rebuild page (>= 0.9
 *  green, >= gate yellow, else red) — kept as a colored number rather than a
 *  `GaugeCell` since the pre-rebuild page never used a bar for this column. */
function confColor(c: number, gate: number): string {
  if (c >= 0.9) return statusColor("ok");
  if (c >= gate) return statusColor("warn");
  return statusColor("error");
}

function gateLabel(c: number, gate: number): string {
  return c >= gate ? "approvable" : `below ${gate.toFixed(2)} gate`;
}

/** Detail panel for a single integration proposal, shown inside the
 *  DetailDrawer on row click. Approve/Reject buttons POST to the
 *  governance-intelligence proposal endpoints and update parent state on
 *  success — same interaction shape as `Remed.tsx`'s `RemedDetail` (this
 *  page's own approve/reject UI was originally copied from that component
 *  in the Phase 0 pass, per TRK-198; this rebuild keeps that lineage, now on
 *  the Phase 1 primitives).
 *
 *  Endpoints (governance_intelligence.py):
 *    POST /api/dashboard/integration_proposals/{id}/approve  (body: {})
 *      — approves AND immediately wires the proposal inline; the response
 *        status ends up "wired" on success (CoverageAgent.wire() sets it),
 *        not "approved" — an approve that doesn't also wire is a 500 with
 *        the proposal reverted to pending server-side.
 *    POST /api/dashboard/integration_proposals/{id}/reject   (no body)
 */
function IntpropDetail({
  item,
  onStatusChange,
  gate,
}: {
  item: Proposal;
  onStatusChange: (id: string, newStatus: string) => void;
  gate: number;
}) {
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirmingReject, setConfirmingReject] = useState(false);

  const isPending = item.status === "pending";

  async function handleApprove() {
    setBusy(true);
    setMsg("Approving and wiring…");
    try {
      const res = await apiPost<{ approved: boolean; wired: boolean }>(
        `/api/dashboard/integration_proposals/${encodeURIComponent(item.id)}/approve`,
        {},
      );
      onStatusChange(item.id, res.wired ? "wired" : "approved");
      setMsg("Approved and wired.");
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
      await apiPost(`/api/dashboard/integration_proposals/${encodeURIComponent(item.id)}/reject`);
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
      <span
        style={{
          fontSize: 12,
          color: "var(--ib-text)",
          fontFamily: "var(--ib-mono, 'DM Mono', monospace)",
          wordBreak: "break-all",
        }}
      >
        {value}
      </span>
    </div>
  );

  return (
    <div style={{ padding: "4px 0" }}>
      <div style={{ marginBottom: 16 }}>
        <Badge tone="neutral">{item.type}</Badge>
      </div>

      {row("Endpoint", item.endpoint)}
      {row("Source", item.source)}
      {row(
        "Confidence",
        <span style={{ color: confColor(item.confidence, gate) }}>
          {item.confidence.toFixed(2)} ({Math.round(item.confidence * 100)}%) · {gateLabel(item.confidence, gate)}
        </span>,
      )}
      {row("Status", <Badge tone={statusTone(item.status)}>{item.status}</Badge>)}
      {item.proposed_at && row("Proposed", fmtDt(item.proposed_at))}

      {isPending && item.confidence < gate && (
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
          Confidence below {gate.toFixed(2)} gate — approving is still possible but not recommended.
        </div>
      )}

      {isPending && (
        <div style={{ display: "flex", gap: 8, marginTop: 16, alignItems: "center" }}>
          <Button variant="primary" size="md" onClick={handleApprove} loading={busy}>
            Approve &amp; Wire
          </Button>
          <Button variant={confirmingReject ? "danger" : "secondary"} size="md" onClick={handleReject} loading={busy}>
            {confirmingReject ? "Click again to confirm" : "Reject"}
          </Button>
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

/** Port of intprops.dc.html (legacy) — integration-discovery proposal review,
 *  backed by GET /api/dashboard/integration_proposals
 *  (src/infra_brain/api/routers/governance_intelligence.py:136), returning
 *  IntegrationProposalPageOut {items,total,limit,offset}. Legacy loaded once
 *  (limit implicit 200, this port uses limit=500 like Remed.tsx) and filtered
 *  client-side by tab; stat tiles reflect the whole set, not just the active
 *  tab, same as before.
 *
 *  Rebuilt on the Phase 1 design system (see dashboard-app/DESIGN.md):
 *  PageShell/StatTile/Panel/DataTable/Badge/Tabs instead of hand-stamped
 *  inline-styled divs. Row click still opens a DetailDrawer with full
 *  proposal detail and Approve & Wire / Reject action buttons — the same
 *  real state-changing operations wired to governance_intelligence.py's
 *  endpoints (TRK-198), now built on the shared `Button` primitive:
 *    POST /api/dashboard/integration_proposals/{id}/approve  (body: {})
 *    POST /api/dashboard/integration_proposals/{id}/reject   (no body)
 *  and optimistically updating the in-memory item status on success. */
type SettingRow = { k: string; type: string; v?: string | null; on?: boolean | null };

export function Intprops() {
  const [all, setAll] = useState<Proposal[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState("pending");
  const [selected, setSelected] = useState<Proposal | null>(null);
  const [gate, setGate] = useState(DEFAULT_CONFIDENCE_GATE);
  const triggerRef = useRef<HTMLDivElement>(null);

  const load = useCallback(async () => {
    await Promise.all([
      apiGet<unknown>("/api/dashboard/integration_proposals?limit=500"),
      apiGet<unknown>("/api/dashboard/settings/ui"),
    ])
      .then(([proposals, settings]) => {
        setAll(rows<Proposal>(proposals));
        const gateRow = rows<SettingRow>(settings).find((r) => r.k === "INTEGRATION_CONFIDENCE_GATE");
        const parsed = gateRow?.v ? Number(gateRow.v) : NaN;
        if (!Number.isNaN(parsed)) setGate(parsed);
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

  /** Called by IntpropDetail when an approve/reject POST succeeds — update
   *  the in-memory list so stats and the open drawer both reflect the change. */
  function handleStatusChange(id: string, newStatus: string) {
    setAll((prev) => (prev ? prev.map((p) => (p.id === id ? { ...p, status: newStatus } : p)) : prev));
    setSelected((prev) => (prev && prev.id === id ? { ...prev, status: newStatus } : prev));
  }

  const h = headerFor("intprops");

  if (error) {
    return (
      <div>
        <PageShell icon={<Icon name={h.icon} />} title={h.title} description={h.desc} />
        <EmptyState kind="error" title="Integration proposals failed to load" hint={error} />
      </div>
    );
  }

  if (all === null) {
    return (
      <div>
        <PageShell icon={<Icon name={h.icon} />} title={h.title} description={h.desc} />
        <Panel mnemonic="INTPROP" description="integration_proposals" rail="info">
          <div style={{ padding: 16 }}>
            <Skeleton count={3} height={32} />
          </div>
        </Panel>
      </div>
    );
  }

  const pendingCount = all.filter((p) => p.status === "pending").length;
  const approvedCount = all.filter((p) => p.status === "approved").length;
  const wiredCount = all.filter((p) => p.status === "wired").length;
  const rejectedCount = all.filter((p) => p.status === "rejected").length;

  const stats: Array<{ label: string; value: number; status: string }> = [
    { label: "Pending", value: pendingCount, status: "pending" },
    { label: "Approved", value: approvedCount, status: "approved" },
    { label: "Wired", value: wiredCount, status: "wired" },
    { label: "Rejected", value: rejectedCount, status: "rejected" },
  ];

  const filtered = status === "(all)" ? all : all.filter((p) => p.status === status);
  const hasFilter = status !== "(all)";
  const empty = filtered.length === 0;

  const columns: ColumnDef<Proposal>[] = [
    { key: "endpoint", header: "Endpoint", typeGlyph: "pk" },
    {
      key: "type",
      header: "Type",
      typeGlyph: "enum",
      render: (p) => <Badge tone="neutral">{p.type}</Badge>,
    },
    {
      key: "confidence",
      header: "Confidence",
      typeGlyph: "num",
      accessor: (p) => p.confidence,
      render: (p) => (
        <span style={{ color: confColor(p.confidence, gate) }}>
          {p.confidence.toFixed(2)} <span style={{ color: "var(--ib-faint)", fontSize: 11 }}>({gateLabel(p.confidence, gate)})</span>
        </span>
      ),
    },
    { key: "source", header: "Source", typeGlyph: "text" },
    {
      key: "proposed_at",
      header: "Proposed",
      typeGlyph: "text",
      render: (p) => fmtDt(p.proposed_at) ?? "—",
    },
    {
      key: "status",
      header: "Status",
      typeGlyph: "enum",
      render: (p) => <Badge tone={statusTone(p.status)}>{p.status}</Badge>,
    },
  ];

  return (
    <div>
      <PageShell icon={<Icon name={h.icon} />} title={h.title} description={h.desc} />

      <div style={{ display: "grid", gridTemplateColumns: "repeat(4,1fr)", gap: 12, marginBottom: 16 }}>
        {stats.map((s) => (
          <StatTile key={s.label} label={s.label} value={s.value} color={statusStatColor(s.status)} />
        ))}
      </div>

      <div style={{ marginBottom: 16 }}>
        <Tabs tabs={TABS} active={status} onChange={setStatus} label="Integration proposal status" />
      </div>

      <Panel mnemonic="INTPROP" description="integration_proposals" rail="info" live>
        {empty ? (
          hasFilter ? (
            <EmptyState
              kind="filter-zero"
              title="No proposals in this status"
              hint="Switch tabs to see other proposals."
              action={{ label: "Show all", onClick: () => setStatus("(all)") }}
            />
          ) : (
            <EmptyState kind="none-yet" title="No integration proposals yet" hint="Proposals appear here after the next discovery sweep." />
          )
        ) : (
          <DataTable<Proposal>
            columns={columns}
            rows={filtered}
            rowKey={(p) => p.id}
            sortable
            toolbarName="integration_proposals"
            caption="Integration proposals: endpoint, type, confidence, source, proposed date, and status"
            onRowClick={(p) => setSelected(p)}
            statusBar={{ shown: filtered.length, total: all.length, note: "read-only · no mutations possible from this view" }}
          />
        )}
      </Panel>

      <DetailDrawer
        title={selected ? `${selected.type} — ${selected.endpoint}` : "Integration proposal detail"}
        open={selected !== null}
        onClose={() => setSelected(null)}
        triggerRef={triggerRef as React.RefObject<HTMLElement>}
      >
        {selected && <IntpropDetail item={selected} onStatusChange={handleStatusChange} gate={gate} />}
      </DetailDrawer>
    </div>
  );
}

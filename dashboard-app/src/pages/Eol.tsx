import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { apiGet, apiPatch, rows, AuthRequired, ApiError, redirectToLogin } from "../api";
import { Skeleton } from "../components/Skeleton";
import { headerFor } from "../lib/headers";
import { useAutoRefresh } from "../hooks/useAutoRefresh";
import {
  PageShell,
  StatTile,
  Panel,
  DataTable,
  Button,
  EmptyState,
  Badge,
  FkCell,
  GaugeCell,
  type ColumnDef,
  type PanelRail,
} from "../components/ui";
import type { Severity } from "../lib/tokens";

/** Proximity band computed server-side (hosts.py list_eol) — NOT a binary
 *  overdue/non-overdue flag. "unknown" is a null eol_date; "tracked" is an
 *  eol_date further out than the approaching window; see hosts.py for the
 *  exact threshold. */
type EolStatus = "overdue" | "approaching" | "tracked" | "unknown";

type EolAsset = {
  id: string;
  asset: string;
  host: string;
  eol: string;
  /** EolRegistry.pci_risk_score verbatim, 0-100 (see agents/eol.py::
   *  EOLAgent._pci_risk_score: past-EOL=90, <90 days=70, <1yr=40, else=10).
   *  Named `pci_risk_score`, not the bare `risk` used before TRK-275/GitLab
   *  #146+#153, and NOT the same field/scale as Rapid7's `risk_score` (an
   *  unbounded exposure score shown on Hosts/Fleet/R7detail, typically in the
   *  hundreds/thousands) — the two must never be conflated.
   *  Nullable: an asset can be entirely unscored (hosts.py sends the raw
   *  pci_risk_score, no `|| 0` default) — treat null as "no score", never as
   *  a score of 0. */
  pci_risk_score: number | null;
  migration: string;
  status: EolStatus;
};

/** Response of PATCH /api/dashboard/eol/{id}/migration (hosts.py:385). */
type EolMigrationResult = { id: string; migration_path: string };

/** Port of legacy EOL page (dashboard/src/pages/eol.dc.html:8-32), rebuilt on
 *  the Phase 1 design system (see dashboard-app/DESIGN.md) as a Phase 3
 *  DataTable conversion. Backed by GET /api/dashboard/eol (hosts.py:318) —
 *  {items,total,...}.
 *
 *  DESIGN CALL (this rebuild): EOL proximity (overdue/approaching/tracked/
 *  unknown) is a status axis, not a severity axis — it describes how close an
 *  asset is to end-of-life, not how bad a finding is — so it renders via
 *  `Badge` (tone-based) rather than `SeverityPill`. Tone mapping:
 *  overdue→err (red), approaching→warn (yellow), tracked→info (blue),
 *  unknown→neutral (muted gray) — this is a 1:1 port of the legacy
 *  eolBadge() palette (red/amber/blue/gray) onto the new Badge tone
 *  vocabulary, not a new scheme.
 *
 *  STRUCTURE CALL: the legacy page rendered one stacked `.ib-card` per asset.
 *  Per DESIGN.md §8 ("never render a list of records as a stack of cards to
 *  look modern" — EOL assets are homogeneous rows sharing the same six
 *  fields), this rebuild converts the list into a `DataTable`. The registry
 *  size (up to the ?limit=200 page cap) also benefits from the table's
 *  density/sort affordances that a card stack couldn't offer.
 *
 *  Every real feature is preserved: the 4-band proximity classification
 *  (TRK-185), the true-registry-total "Tracked" tile and "showing X of Y"
 *  capped note (Bug 2), the risk average that excludes unscored assets from
 *  its denominator (Bug 3), and the admin-gated inline migration-path editor
 *  (pencil → input/Save/Cancel, 403 → inline "not permitted" notice).
 *
 *  v1 parity (T1.8): the migration path is inline-editable per row via a pencil
 *  affordance. Editing PATCHes /api/dashboard/eol/{id}/migration, which is
 *  admin-gated (require_admin, hosts.py:377). */
export function Eol() {
  const navigate = useNavigate();
  const [assets, setAssets] = useState<EolAsset[] | null>(null);
  // True registry row count (hosts.py EolPageOut.total), distinct from
  // assets.length once the registry exceeds the ?limit=200 page cap (Bug 2,
  // 2026-07-24 UX audit T6).
  const [total, setTotal] = useState<number>(0);
  const [error, setError] = useState<string | null>(null);

  // Single-row inline edit state: which row (id) is being edited, the draft
  // migration-path text, and a per-row "not permitted" (403) inline notice.
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraft] = useState<string>("");
  const [denied, setDenied] = useState<string | null>(null);

  const load = useCallback(async () => {
    await apiGet<unknown>("/api/dashboard/eol?limit=200")
      .then((d) => {
        const items = rows<EolAsset>(d);
        setAssets(items);
        const envelope = d as { total?: number };
        setTotal(envelope?.total ?? items.length);
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

  const startEdit = useCallback((e: EolAsset) => {
    setDenied(null);
    setEditingId(e.id);
    setDraft(e.migration === "—" ? "" : e.migration);
  }, []);

  const cancelEdit = useCallback(() => {
    setEditingId(null);
    setDraft("");
  }, []);

  const saveEdit = useCallback(
    async (id: string) => {
      const next = draft.trim();
      try {
        const res = await apiPatch<EolMigrationResult>(`/api/dashboard/eol/${id}/migration`, {
          migration_path: next,
        });
        // Success: reflect the server-normalized value in local state.
        setAssets((prev) =>
          prev ? prev.map((a) => (a.id === id ? { ...a, migration: res.migration_path || "—" } : a)) : prev,
        );
        setEditingId(null);
        setDraft("");
        setDenied(null);
      } catch (err) {
        if (err instanceof AuthRequired) {
          // 401 — session expired: bounce to login (never crash the page).
          redirectToLogin();
          return;
        }
        if (err instanceof ApiError && err.status === 403) {
          // 403 — authenticated but not an admin: revert to display mode and
          // show a non-destructive inline notice. No data is lost or mutated.
          setEditingId(null);
          setDraft("");
          setDenied(id);
          return;
        }
        // Any other failure: surface via the page-level error banner.
        setError(String(err));
      }
    },
    [draft],
  );

  const h = headerFor("eol");

  if (error) {
    return (
      <div>
        <PageShell title={h.title} description={h.desc} />
        <EmptyState kind="error" title="EOL registry failed to load" hint={error} />
      </div>
    );
  }

  if (assets === null) {
    return (
      <div>
        <PageShell title={h.title} description={h.desc} />
        <Skeleton count={4} height={70} />
      </div>
    );
  }

  const overdue = assets.filter((e) => e.status === "overdue").length;
  // Approaching reflects the real proximity band from hosts.py — assets that
  // are merely "tracked" (EOL years out) or "unknown" (no eol_date) are not
  // folded into this count (Bug 1, 2026-07-24 UX audit T6).
  const approaching = assets.filter((e) => e.status === "approaching").length;
  // Avg PCI Risk: exclude unscored assets (pci_risk_score === null) from the
  // denominator entirely rather than treating "unscored" as "zero risk"
  // (Bug 3, 2026-07-24 UX audit T6).
  const scored = assets.filter(
    (e): e is EolAsset & { pci_risk_score: number } => e.pci_risk_score !== null,
  );
  const avgRisk = scored.length
    ? (scored.reduce((a, e) => a + e.pci_risk_score, 0) / scored.length).toFixed(1)
    : "—";
  const capped = total > assets.length;

  const stats: Array<{ label: string; value: string; color: "red" | "yellow" | "green" | "blue" | undefined }> = [
    // True registry total (not the ?limit=200 page length) — Bug 2.
    { label: "Tracked", value: String(total), color: "blue" },
    { label: "Overdue", value: String(overdue), color: "red" },
    { label: "Approaching", value: String(approaching), color: "yellow" },
    { label: "Avg PCI Risk", value: avgRisk, color: "yellow" },
  ];

  const rail: PanelRail = assets.some((a) => a.status === "overdue")
    ? "err"
    : assets.some((a) => a.status === "approaching")
      ? "warn"
      : "info";

  const columns: ColumnDef<EolAsset>[] = [
    { key: "asset", header: "Asset", typeGlyph: "pk" },
    {
      key: "host",
      header: "Host",
      typeGlyph: "fk",
      // Cross-page navigation to the unified Hosts view, filtered to this
      // host. `?host=` is read by nothing (Hosts.tsx only reads `?tab=`,
      // UnifiedHostsTab.tsx only reads `?q=`) and a raw `href` escapes the
      // router's `basename="/dashboard2"` into a hard-navigation 404 against
      // the backend (F-1) — onClick+navigate with `?tab=hosts&q=...` fixes
      // both, matching Vulns.tsx/Invrec.tsx's identical host->Hosts drill-down.
      render: (e) => (
        <FkCell label={e.host} onClick={() => navigate(`/hosts?tab=hosts&q=${encodeURIComponent(e.host)}`)} />
      ),
    },
    { key: "eol", header: "EOL Date", typeGlyph: "text" },
    {
      key: "status",
      header: "Status",
      typeGlyph: "enum",
      render: (e) => <Badge tone={eolTone(e.status)}>{e.status}</Badge>,
    },
    {
      key: "pci_risk_score",
      header: "PCI Risk Score",
      typeGlyph: "num",
      // Unscored (null) assets render the automatic NULL token — a bare 0
      // would misreport "no score" as "zero risk" (same Bug 3 principle
      // applied per-row, not just in the Avg PCI Risk aggregate above).
      // pci_risk_score is already 0-100 (see EOLAgent._pci_risk_score) — no
      // ×10 rescale, unlike CVSS (0-10) cells elsewhere in the dashboard.
      render: (e) =>
        e.pci_risk_score != null ? (
          <GaugeCell
            value={e.pci_risk_score}
            severity={riskSeverity(e.pci_risk_score)}
            display={`${e.pci_risk_score}/100`}
            label="PCI risk score"
          />
        ) : null,
    },
    {
      key: "migration",
      header: "Migration Path",
      typeGlyph: "text",
      sortable: false,
      render: (e) => {
        const isEditing = editingId === e.id;
        if (isEditing) {
          return (
            <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
              <input
                autoFocus
                value={draft}
                onChange={(ev) => setDraft(ev.target.value)}
                onKeyDown={(ev) => {
                  if (ev.key === "Enter") void saveEdit(e.id);
                  else if (ev.key === "Escape") cancelEdit();
                }}
                style={{
                  background: "var(--ib-raised)",
                  border: "1px solid var(--ib-blue)",
                  borderRadius: "var(--ib-radius-sm)",
                  color: "var(--ib-text)",
                  fontSize: 11,
                  fontFamily: "var(--ib-mono, 'DM Mono', monospace)",
                  padding: "2px 6px",
                  width: 200,
                }}
              />
              <Button size="sm" variant="primary" onClick={() => void saveEdit(e.id)}>
                Save
              </Button>
              <Button size="sm" variant="ghost" onClick={cancelEdit}>
                Cancel
              </Button>
            </span>
          );
        }
        return (
          <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            <span>{e.migration}</span>
            <button
              onClick={() => startEdit(e)}
              aria-label="Edit migration path"
              style={{ background: "none", border: "none", color: "var(--ib-faint)", cursor: "pointer", fontSize: 11, padding: 0 }}
            >
              {"✎"}
            </button>
            {denied === e.id && <Badge tone="warn">not permitted</Badge>}
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

      {capped && (
        <div style={{ fontSize: 11, color: "var(--ib-yellow)", fontFamily: "var(--ib-mono, 'DM Mono', monospace)", marginBottom: 12 }}>
          showing {assets.length} of {total} EOL assets
        </div>
      )}

      <Panel rail={rail} mnemonic="EOL" description="host_eol_registry" live>
        {assets.length === 0 ? (
          <EmptyState
            kind="none-yet"
            title="No end-of-life assets tracked"
            hint="Assets will appear here after the next EOL agent sweep."
          />
        ) : (
          <DataTable<EolAsset>
            columns={columns}
            rows={assets}
            rowKey={(e) => e.id}
            sortable
            toolbarName="host_eol_registry"
            caption="End-of-life registry: asset, host, EOL date, proximity status, PCI risk, and migration path"
            statusBar={{ shown: assets.length, total }}
          />
        )}
      </Panel>
    </div>
  );
}

/** Proximity band → Badge tone. `overdue` (most critical) → err, `approaching`
 *  → warn, `tracked` (further out than the approaching window) → info,
 *  `unknown` (no eol_date on record) → neutral. Ported verbatim from the
 *  pre-rebuild eolBadge() palette (red/amber/blue/gray, eol.dc.html:2215) —
 *  the red overdue signal is load-bearing and must not be recolored. */
function eolTone(status: EolStatus): "err" | "warn" | "info" | "neutral" {
  if (status === "overdue") return "err";
  if (status === "approaching") return "warn";
  if (status === "tracked") return "info";
  return "neutral";
}

/** PCI risk score (0-100, EOLAgent._pci_risk_score) → gauge hue. Thresholds
 *  line up with the score's own 4-tier scale (past-EOL=90, <90 days=70,
 *  <1yr=40, else=10): 70+ (past/near-term) → critical, 40 (within a year) →
 *  medium, 10 (no near-term EOL) → low. TRK-275/GitLab #146+#153: the prior
 *  thresholds (>=8 / >=5) assumed a 0-10 scale that the score never actually
 *  used, so every real score (10/40/70/90) was >=8 and rendered "critical"
 *  regardless of actual urgency — this rescale is the fix, not a cosmetic
 *  tweak. */
function riskSeverity(risk: number): Severity {
  if (risk >= 70) return "critical";
  if (risk >= 40) return "medium";
  return "low";
}

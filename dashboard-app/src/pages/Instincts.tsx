import { useCallback, useEffect, useState } from "react";
import { apiGet, rows, AuthRequired, redirectToLogin } from "../api";
import { Icon } from "../lib/icons";
import { headerFor } from "../lib/headers";
import { useAutoRefresh } from "../hooks/useAutoRefresh";
import { Skeleton } from "../components/Skeleton";
import {
  PageShell,
  StatTile,
  Panel,
  DataTable,
  Badge,
  EmptyState,
  type ColumnDef,
} from "../components/ui";
import { statusColor } from "../lib/tokens";
import { fmtDt } from "../lib/fmtDt";

type Instinct = {
  domain: string;
  zone: string;
  pattern: string;
  confidence: number;
  promoted_at: string;
  applied: boolean;
};

const DOMAIN_OPTS = ["(all)", "linux", "cloud", "k8s", "windows", "onprem", "net", "vuln"];
// "corpor" (ZONE_CORPORATE) is the only real zone literal in the backend
// (db/models/core.py / config.py's default_zone) — the former "in-zone"
// entry didn't exist anywhere in the data, so selecting it always produced a
// misleading "adjust your filters" empty state.
const ZONE_OPTS = ["(all)", "corpor"];

function qs(params: Record<string, string>): string {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v) sp.set(k, v);
  const s = sp.toString();
  return s ? `?${s}` : "";
}

/** Confidence → color, 3 tiers exactly like the pre-rebuild page (>= 0.9
 *  green, >= 0.7 yellow, else muted) — deliberately not the 2-tier
 *  ok/warn split other pages use, and not `GaugeCell`'s severity axis
 *  (severity's "low" band renders `--ib-faint`, not green, which would
 *  invert the meaning here: high confidence is good, not low-severity). */
function confColor(c: number): string {
  if (c >= 0.9) return statusColor("ok");
  if (c >= 0.7) return statusColor("warn");
  return "var(--ib-faint)";
}

const selectStyle: React.CSSProperties = {
  background: "var(--ib-raised)",
  border: "1px solid var(--ib-border)",
  borderRadius: "var(--ib-radius-sm)",
  padding: "6px 12px",
  color: "var(--ib-text)",
  fontSize: 13,
  fontFamily: "inherit",
  outline: "none",
  cursor: "pointer",
};

/** Port of instincts.dc.html (legacy) — learned-pattern list backed by
 *  GET /api/dashboard/instincts (src/infra_brain/api/routers/governance.py:257),
 *  which returns InstinctPageOut {items,total,limit,offset} and now supports
 *  domain/zone as server-side query params (the legacy UI loaded everything
 *  once and filtered client-side; this port filters server-side instead —
 *  behavior-equivalent, less client work).
 *
 *  Rebuilt on the Phase 1 design system (see dashboard-app/DESIGN.md):
 *  instincts share the same fields across every row (domain, zone, pattern,
 *  confidence, promoted date, applied flag) — a homogeneous grain, so per
 *  `DESIGN.md`'s table-vs-card guidance the pre-rebuild hand-stamped card
 *  stack becomes a `DataTable`, not a `Panel`/card layout. Domain/zone
 *  filters stay plain `<select>`s (same interaction as before), just
 *  recolored off `--ib-*` tokens instead of hardcoded hex. */
export function Instincts() {
  const [items, setItems] = useState<Instinct[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [domain, setDomain] = useState("(all)");
  const [zone, setZone] = useState("(all)");

  const load = useCallback(async () => {
    await apiGet<unknown>(
      "/api/dashboard/instincts" + qs({ domain: domain === "(all)" ? "" : domain, zone: zone === "(all)" ? "" : zone }),
    )
      .then((d) => setItems(rows<Instinct>(d)))
      .catch((e) => {
        if (e instanceof AuthRequired) redirectToLogin();
        else setError(String(e));
      });
  }, [domain, zone]);

  useEffect(() => {
    void load();
  }, [load]);
  useAutoRefresh(load);

  const h = headerFor("instincts");

  if (error) {
    return (
      <div>
        <PageShell icon={<Icon name={h.icon} />} title={h.title} description={h.desc} />
        <EmptyState kind="error" title="Instincts failed to load" hint={error} />
      </div>
    );
  }

  if (items === null) {
    return (
      <div>
        <PageShell icon={<Icon name={h.icon} />} title={h.title} description={h.desc} />
        <Panel mnemonic="INSTINCT" description="instincts" rail="info">
          <div style={{ padding: 16 }}>
            <Skeleton count={3} height={32} />
          </div>
        </Panel>
      </div>
    );
  }

  // Use the backend's authoritative `applied` field rather than recomputing
  // a >= 0.7 threshold client-side — the backend threshold could change
  // without this page's copy of it noticing.
  const appliedCount = items.filter((i) => i.applied).length;
  const hasFilter = domain !== "(all)" || zone !== "(all)";
  const empty = items.length === 0;

  const stats = [
    { label: "Instincts", value: items.length, color: undefined as "blue" | undefined },
    { label: "Applied", value: appliedCount, color: "blue" as const },
  ];

  const columns: ColumnDef<Instinct>[] = [
    {
      key: "domain",
      header: "Domain",
      typeGlyph: "enum",
      render: (i) => <Badge tone="info">{i.domain}</Badge>,
    },
    { key: "zone", header: "Zone", typeGlyph: "enum" },
    { key: "pattern", header: "Pattern", typeGlyph: "text" },
    {
      key: "confidence",
      header: "Confidence",
      typeGlyph: "num",
      accessor: (i) => i.confidence,
      render: (i) => (
        <span style={{ display: "inline-flex", alignItems: "center", gap: 9 }}>
          <span
            style={{
              width: 40,
              height: 4,
              borderRadius: 2,
              background: "var(--ib-border)",
              overflow: "hidden",
              display: "inline-block",
            }}
          >
            <span
              style={{
                display: "block",
                height: "100%",
                width: `${Math.round(i.confidence * 100)}%`,
                background: confColor(i.confidence),
                borderRadius: 2,
              }}
            />
          </span>
          <span style={{ color: confColor(i.confidence), fontWeight: 600 }}>{i.confidence.toFixed(2)}</span>
        </span>
      ),
    },
    {
      key: "promoted_at",
      header: "Promoted",
      typeGlyph: "text",
      render: (i) => fmtDt(i.promoted_at) ?? "—",
    },
    {
      key: "applied",
      header: "Applied",
      typeGlyph: "enum",
      render: (i) => (i.applied ? <Badge tone="ok">applied to reasoning</Badge> : <span style={{ color: "var(--ib-faint)" }}>—</span>),
    },
  ];

  return (
    <div>
      <PageShell icon={<Icon name={h.icon} />} title={h.title} description={h.desc} />

      <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 16, flexWrap: "wrap" }}>
        <select value={domain} onChange={(e) => setDomain(e.target.value)} style={selectStyle}>
          {DOMAIN_OPTS.map((o) => (
            <option key={o} value={o}>
              {o}
            </option>
          ))}
        </select>
        <select value={zone} onChange={(e) => setZone(e.target.value)} style={selectStyle}>
          {ZONE_OPTS.map((o) => (
            <option key={o} value={o}>
              {o}
            </option>
          ))}
        </select>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(2,1fr)", gap: 12, marginBottom: 16, maxWidth: 340 }}>
        {stats.map((s) => (
          <StatTile key={s.label} label={s.label} value={s.value} color={s.color} />
        ))}
      </div>

      <Panel mnemonic="INSTINCT" description="instincts" rail="info" live>
        {empty ? (
          hasFilter ? (
            <EmptyState
              kind="filter-zero"
              title="No instincts match your filters"
              hint="Adjust the filters above to see results."
              action={{
                label: "Clear filters",
                onClick: () => {
                  setDomain("(all)");
                  setZone("(all)");
                },
              }}
            />
          ) : (
            <EmptyState kind="none-yet" title="No instincts learned yet" hint="Instincts appear here once the drift-history learning loop promotes a pattern." />
          )
        ) : (
          <DataTable<Instinct>
            columns={columns}
            rows={items}
            rowKey={(i, idx) => `${i.domain}-${i.zone}-${idx}`}
            sortable
            toolbarName="instincts"
            caption="Learned instincts: domain, zone, pattern, confidence, promoted date, and applied state"
            statusBar={{ shown: items.length, note: "read-only · no mutations possible from this view" }}
          />
        )}
      </Panel>
    </div>
  );
}

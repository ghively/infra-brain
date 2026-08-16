import { useCallback, useEffect, useRef, useState } from "react";
import { apiGet, rows, AuthRequired, redirectToLogin } from "../api";
import { Skeleton } from "../components/Skeleton";
import { headerFor } from "../lib/headers";
import { Icon } from "../lib/icons";
import { useAutoRefresh } from "../hooks/useAutoRefresh";
import {
  PageShell,
  StatTile,
  Panel,
  DataTable,
  Button,
  EmptyState,
  Tabs,
  type ColumnDef,
} from "../components/ui";

/** Port of the Software page (legacy dashboard/src/pages/software.dc.html:10-78,
 *  driven by _loadSoftware() (src/infra_brain/dashboard/static/index.html:4075-4091)
 *  and renderVals() (same file, lines ~7035-7076). Rebuilt on the Phase 1 design
 *  system (see dashboard-app/DESIGN.md) as a Phase 3 mechanical DataTable/Panel
 *  conversion — no behavior change from the pre-rebuild version.
 *
 *  /api/dashboard/software returns SoftwareInventoryOut
 *  {view,items,total,limit,offset,summary} (src/infra_brain/api/routers/fleet.py:513).
 *  Filtering (q), paging, and the aggregated/detail view toggle are all
 *  server-side, same division of labor as Vulns.
 *
 *  Simplification: the legacy markup declares a vendor filter chip bar
 *  (swVendorChips/swHasVendors, software.dc.html:16) and the backend exposes
 *  a matching GET /software/vendors endpoint (fleet.py:491), but renderVals()
 *  never actually computes swVendorChips/swHasVendors — those holes are only
 *  ever fed their `hint-placeholder-val="{{ false }}"` default, so the chip
 *  bar is dead in production today. This port omits it too, to stay
 *  behavior-identical with what legacy actually renders. */

type SoftwareDetailRow = {
  id: string;
  r7_asset_id: number | null;
  hostname: string;
  product: string;
  version: string;
  vendor: string;
  software_type: string;
};

type SoftwareAggRow = {
  product: string;
  version: string;
  vendor: string;
  host_count: number;
};

type SoftwareSummary = {
  total_records: number;
  unique_products: number;
  hosts_covered: number;
};

const PAGE_SIZE = 50;

type ViewMode = "aggregated" | "detail";

const VIEW_TABS: Array<{ key: ViewMode; label: string }> = [
  { key: "aggregated", label: "Aggregated" },
  { key: "detail", label: "Per-host detail" },
];

export function Software() {
  const [view, setView] = useState<ViewMode>("aggregated");
  const [detailItems, setDetailItems] = useState<SoftwareDetailRow[] | null>(null);
  const [aggItems, setAggItems] = useState<SoftwareAggRow[] | null>(null);
  const [summary, setSummary] = useState<SoftwareSummary>({
    total_records: 0,
    unique_products: 0,
    hosts_covered: 0,
  });
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [queryInput, setQueryInput] = useState("");
  const [query, setQuery] = useState("");

  const debounceRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  useEffect(() => {
    clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => {
      setOffset(0);
      setQuery(queryInput);
    }, 350);
    return () => clearTimeout(debounceRef.current);
  }, [queryInput]);

  const load = useCallback(async () => {
    setLoading(true);
    const qs = new URLSearchParams();
    qs.set("view", view);
    if (query) qs.set("q", query);
    qs.set("limit", String(PAGE_SIZE));
    qs.set("offset", String(offset));
    try {
      const d = await apiGet<unknown>(`/api/dashboard/software?${qs.toString()}`);
      const envelope = d as { total?: number; summary?: SoftwareSummary };
      if (view === "detail") {
        setDetailItems(rows<SoftwareDetailRow>(d));
        setAggItems(null);
      } else {
        setAggItems(rows<SoftwareAggRow>(d));
        setDetailItems(null);
      }
      setTotal(typeof envelope?.total === "number" ? envelope.total : 0);
      setSummary(envelope?.summary ?? { total_records: 0, unique_products: 0, hosts_covered: 0 });
      setError(null);
    } finally {
      setLoading(false);
    }
  }, [view, query, offset]);

  useEffect(() => {
    load().catch((e) => {
      if (e instanceof AuthRequired) redirectToLogin();
      else setError(String(e));
    });
  }, [load]);

  useAutoRefresh(load);

  const h = headerFor("software");

  if (error) {
    return (
      <div>
        <PageShell icon={<Icon name={h.icon} />} title={h.title} description={h.desc} />
        <EmptyState kind="error" title="Software inventory failed to load" hint={error} />
      </div>
    );
  }

  const isDetail = view === "detail";
  const noDataLoaded = isDetail ? detailItems === null : aggItems === null;

  if (noDataLoaded) {
    return (
      <div>
        <PageShell icon={<Icon name={h.icon} />} title={h.title} description={h.desc} />
        <Skeleton count={3} height={32} />
      </div>
    );
  }

  const shown = isDetail ? (detailItems ?? []).length : (aggItems ?? []).length;

  const stats: Array<{ label: string; value: number; color: "red" | "yellow" | "green" | "blue" | undefined }> = [
    { label: "Total Records", value: summary.total_records, color: undefined },
    { label: "Unique Products", value: summary.unique_products, color: "blue" },
    { label: "Hosts Covered", value: summary.hosts_covered, color: "green" },
  ];

  const pageStart = total === 0 ? 0 : offset + 1;
  const pageEnd = Math.min(offset + shown, total);
  const hasPrev = offset > 0;
  const hasNext = offset + shown < total;
  const hasAnyFilter = query.trim() !== "";
  const empty = shown === 0 && !loading;

  const detailColumns: ColumnDef<SoftwareDetailRow>[] = [
    { key: "hostname", header: "Hostname", typeGlyph: "fk" },
    { key: "product", header: "Product", typeGlyph: "text" },
    { key: "version", header: "Version", typeGlyph: "text" },
    { key: "vendor", header: "Vendor", typeGlyph: "text" },
    { key: "software_type", header: "Type", typeGlyph: "enum" },
  ];

  const aggColumns: ColumnDef<SoftwareAggRow>[] = [
    { key: "product", header: "Product", typeGlyph: "text" },
    { key: "version", header: "Version", typeGlyph: "text" },
    { key: "vendor", header: "Vendor", typeGlyph: "text" },
    { key: "host_count", header: "Hosts", typeGlyph: "num" },
  ];

  return (
    <div>
      <PageShell icon={<Icon name={h.icon} />} title={h.title} description={h.desc} />

      <div style={{ display: "grid", gridTemplateColumns: "repeat(3,1fr)", gap: 12, marginBottom: 16 }}>
        {stats.map((s) => (
          <StatTile key={s.label} label={s.label} value={s.value} color={s.color} />
        ))}
      </div>

      <div style={{ display: "flex", gap: 10, marginBottom: 14, flexWrap: "wrap", alignItems: "center" }}>
        <Tabs
          tabs={VIEW_TABS.map((t) => ({ key: t.key, label: t.label }))}
          active={view}
          onChange={(key) => {
            setView(key as ViewMode);
            setOffset(0);
          }}
          label="Software view"
        />
        <input
          value={queryInput}
          onChange={(e) => setQueryInput(e.target.value)}
          placeholder="Search product…"
          style={{
            background: "var(--ib-raised)",
            border: "1px solid var(--ib-border)",
            borderRadius: "var(--ib-radius-sm)",
            padding: "6px 10px",
            color: "var(--ib-text)",
            fontSize: 12,
            minWidth: 200,
            marginLeft: "auto",
            outline: "none",
            fontFamily: "inherit",
          }}
        />
      </div>

      <Panel
        mnemonic="SOFTWARE"
        description={isDetail ? "software_inventory (per-host detail)" : "software_inventory (aggregated by product/version/vendor)"}
        live
      >
        {empty ? (
          hasAnyFilter ? (
            <EmptyState
              kind="filter-zero"
              title="No software records match the current search"
              hint="Try a different product name, or clear the search."
              action={{ label: "Clear search", onClick: () => setQueryInput("") }}
            />
          ) : (
            // Software inventory is collected from Rapid7 (headerFor("software").desc
            // — "collected read-only from Rapid7"), so an empty table on a homelab
            // without Rapid7 InsightVM means the collector was never configured, not
            // "hasn't synced yet". RAPID7_URL / RAPID7_API_KEY default empty in
            // config.py.
            <EmptyState
              kind="not-configured"
              title="No software inventory is being collected yet"
              hint="This page is kept on purpose: software inventory is wanted here, it just has no live collector yet. The original feed was Rapid7 InsightVM, whose collector is retired on this deployment. The Linux path exists in the schema (linux_packages, written by the linux collector's package probe) but has no rows — that probe is gated behind fleet SSH (TRK-099). Once any package source lands, this view fills in with no UI change."
            />
          )
        ) : isDetail ? (
          <DataTable<SoftwareDetailRow>
            columns={detailColumns}
            rows={detailItems ?? []}
            rowKey={(s) => s.id}
            toolbarName="software (detail)"
            caption="Per-host software detail: hostname, product, version, vendor, and type"
            statusBar={{
              shown,
              total,
              note: "read-only · no mutations possible from this view",
            }}
          />
        ) : (
          <DataTable<SoftwareAggRow>
            columns={aggColumns}
            rows={aggItems ?? []}
            rowKey={(s, i) => `${s.product}-${s.version}-${i}`}
            toolbarName="software (aggregated)"
            caption="Aggregated software inventory: product, version, vendor, and host count"
            statusBar={{
              shown,
              total,
              note: "read-only · no mutations possible from this view",
            }}
          />
        )}
      </Panel>

      {!empty && (
        <div style={{ display: "flex", alignItems: "center", justifyContent: "flex-end", gap: 8, marginTop: 12 }}>
          <span style={{ fontSize: 11, color: "var(--ib-faint)", fontFamily: "var(--ib-mono, 'DM Mono', monospace)", marginRight: 6 }}>
            {total === 0 ? "0 rows" : `${pageStart}–${pageEnd} of ${total}`}
          </span>
          {hasPrev && (
            <Button size="sm" onClick={() => setOffset((o) => Math.max(0, o - PAGE_SIZE))}>
              ‹ Prev
            </Button>
          )}
          {hasNext && (
            <Button size="sm" onClick={() => setOffset((o) => o + PAGE_SIZE)}>
              Next ›
            </Button>
          )}
        </div>
      )}
    </div>
  );
}

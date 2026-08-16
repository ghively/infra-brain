/** "Resources" tab of the consolidated Hosts page — the raw per-collector view.
 *
 *  Backed by GET /api/dashboard/resources (ResourcePageOut {items,total,limit,
 *  offset}), filtered server-side by domain / zone / resource_type. The grain is
 *  deliberately different from the Hosts tab: one row per *collector-owned
 *  resource*, so a single physical host appears once per domain that collected it
 *  (linux + vsphere + octopus …), each with its own resource id, snapshot
 *  timeline and drift history. That is why this stayed a distinct tab rather than
 *  being merged into the unified host table — see `Hosts.tsx`'s header.
 *
 *  Row click opens the resource drawer (facts + snapshot timeline + related
 *  drift); a drift row inside it opens a second, self-contained drift drawer.
 *  Domain chips carry page-scoped counts, explicitly labeled "(page)" because
 *  there is no server-side per-domain aggregate to draw from.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { AuthRequired, apiGet, redirectToLogin, rows } from "../../api";
import { DetailDrawer } from "../../components/DetailDrawer";
import { Skeleton } from "../../components/Skeleton";
import { DataTable } from "../../components/ui/DataTable";
import type { ColumnDef } from "../../components/ui/DataTable";
import { EmptyState } from "../../components/ui/EmptyState";
import { Panel } from "../../components/ui/Panel";
import { Badge } from "../../components/ui/SeverityPill";
import { DomainCell, FkCell } from "../../components/ui/cells";
import { useAutoRefresh } from "../../hooks/useAutoRefresh";
import { FAINT, FONT_MONO, MUTED, TEXT, normalizeStatus, statusColor } from "../../lib/tokens";
import { ChipFilter, FilterInput, KvGrid, Pager, SectionLabel, fmtDt, pageWindow } from "./shared";

export type Resource = {
  id: string;
  hostname: string;
  domain: string;
  resource_type: string;
  zone: string;
  status: string;
  last_seen_at: string;
  drift_count: number;
};

type ResourcePage = { items: Resource[]; total: number; limit: number; offset: number };

type Snapshot = { ts: string; label: string };

type Drift = {
  id: string;
  domain: string;
  hostname: string;
  field_name: string;
  old_value: string;
  new_value: string;
  detected_at: string;
  status: string;
  jira_ticket: string;
  drift_type: string;
  root_cause: string;
};

const ZONE_OPTS = [
  { value: "(all)", label: "All zones" },
  { value: "corpor", label: "corpor" },
  { value: "in-zone", label: "in-zone" },
];

const PAGE_SIZE = 200;

/** Resource `status` → badge tone. The backend emits exactly two values
 *  ("eol" when the resource has a past-dated EOL registry entry, else
 *  "healthy" — see `list_resources` in api/routers/hosts.py); anything else is
 *  genuinely unrecognized and must read neutral, never green. */
function statusTone(status: string): "ok" | "err" | "neutral" {
  const s = (status || "").toLowerCase();
  if (s === "healthy") return "ok";
  if (s === "eol") return "err";
  return "neutral";
}

function driftTypeLabel(t: string): string {
  return t === "state_drift" ? "state drift" : "config drift";
}

/** Drift `status` → badge tone. Drift events use a different status vocabulary
 *  than resources ("open"/"acknowledged"/"resolved", not "healthy"/"eol") —
 *  mirrors the mapping in Drift.tsx (`d.status === "open" ? "warn" : d.status
 *  === "resolved" ? "ok" : "neutral"`) so a drift badge here reads the same
 *  severity as it does on the Drift page, instead of always neutral gray via
 *  the resource-only `statusTone`. */
function driftStatusTone(status: string): "ok" | "warn" | "neutral" {
  if (status === "open") return "warn";
  if (status === "resolved") return "ok";
  return "neutral";
}

export function ResourcesTab() {
  const [page, setPage] = useState<ResourcePage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [domain, setDomain] = useState("(all)");
  const [zone, setZone] = useState("(all)");
  const [type, setType] = useState("");
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<Resource | null>(null);
  const [driftSelected, setDriftSelected] = useState<Drift | null>(null);
  const triggerRef = useRef<HTMLElement>(null);
  const driftTriggerRef = useRef<HTMLElement>(null);
  const location = useLocation();
  const navigate = useNavigate();

  const load = useCallback(async () => {
    const params = new URLSearchParams();
    if (domain !== "(all)") params.set("domain", domain);
    if (zone !== "(all)") params.set("zone", zone);
    if (type) params.set("resource_type", type);
    params.set("limit", String(PAGE_SIZE));
    params.set("offset", String(offset));
    await apiGet<unknown>(`/api/dashboard/resources?${params.toString()}`)
      .then((d) => {
        const items = rows<Resource>(d);
        const env = d as Partial<ResourcePage>;
        setPage({ items, total: env.total ?? items.length, limit: PAGE_SIZE, offset });
      })
      .catch((e) => {
        if (e instanceof AuthRequired) redirectToLogin();
        else setError(String(e));
      });
  }, [domain, zone, type, offset]);

  useEffect(() => {
    void load();
  }, [load]);
  useAutoRefresh(load);

  // Cross-page deep-link: the Topbar's global search navigates to /resources
  // with router state { detailOpen: { kind:'resource', data } }. Open the drawer
  // for that datum on mount, then clear the state so it doesn't re-trigger.
  // (The /resources route still exists and still renders this tab — see the
  // routing note in `Hosts.tsx` — precisely so this link keeps working.)
  useEffect(() => {
    const st = location.state as { detailOpen?: { kind: string; data: Resource } } | null;
    if (st?.detailOpen?.kind === "resource" && st.detailOpen.data) {
      setSelected(st.detailOpen.data);
      navigate(location.pathname + location.search, { replace: true, state: null });
    }
  }, [location, navigate]);

  if (error) {
    return (
      <EmptyState
        kind="error"
        title="Resources failed to load"
        hint={error}
        action={{ label: "Retry", onClick: () => { setError(null); void load(); } }}
      />
    );
  }
  if (page === null) return <Skeleton count={6} height={26} />;

  const total = page.total;
  const domains = [...new Set(page.items.map((r) => r.domain))].sort();
  const domCount: Record<string, number> = {};
  for (const r of page.items) domCount[r.domain] = (domCount[r.domain] ?? 0) + 1;
  const eolCount = page.items.filter((r) => r.status === "eol").length;
  const filtersActive = domain !== "(all)" || zone !== "(all)" || type.trim() !== "";

  // Per-domain counts come from the current page's items only — there is no
  // server-side per-domain aggregate for /api/dashboard/resources. With more
  // than one page of results they don't sum to the "(all)" total, so "(page)"
  // makes the scope explicit rather than reading as broken.
  const domainOpts = [
    { value: "(all)", label: `All domains ${total}` },
    ...domains.map((d) => ({ value: d, label: `${d} ${domCount[d] ?? 0} (page)` })),
  ];

  const columns: ColumnDef<Resource>[] = [
    {
      // FK-as-human-label (DESIGN.md §5): a resource row's identity is its uuid,
      // but the uuid is never the primary content — the hostname is, with the id
      // kept dim for copy/paste, and the click-through going to the *unified host
      // identity* this resource rolls up into (the Hosts tab, filtered to it).
      // That is the genuine cross-grain reference on this row; the row itself
      // still opens this resource's own drawer.
      key: "hostname",
      header: "Hostname",
      typeGlyph: "fk",
      render: (r) =>
        r.hostname ? (
          <FkCell
            label={r.hostname}
            id={r.id ? String(r.id).slice(0, 8) : null}
            title={`View ${r.hostname} in the unified Hosts view (resource ${r.id})`}
            onClick={(e) => {
              e.stopPropagation();
              navigate(`/hosts?tab=hosts&q=${encodeURIComponent(r.hostname)}`);
            }}
          />
        ) : null,
    },
    { key: "domain", header: "Domain", typeGlyph: "enum", render: (r) => (r.domain ? <DomainCell name={r.domain} /> : null) },
    { key: "resource_type", header: "Type", typeGlyph: "enum" },
    { key: "zone", header: "Zone", typeGlyph: "enum" },
    {
      key: "drift_count",
      header: "Open drift",
      typeGlyph: "num",
      accessor: (r) => r.drift_count ?? 0,
      render: (r) => {
        const dn = r.drift_count ?? 0;
        return dn > 0 ? <span style={{ color: statusColor("warn") }}>{dn} open</span> : 0;
      },
    },
    {
      key: "status",
      header: "Status",
      typeGlyph: "enum",
      render: (r) => (r.status ? <Badge tone={statusTone(r.status)}>{r.status}</Badge> : null),
    },
    { key: "last_seen_at", header: "Last seen", typeGlyph: "text", render: (r) => fmtDt(r.last_seen_at) },
  ];

  return (
    <div>
      <div className="ib-hosts-filters">
        <ChipFilter
          label="Filter by collector domain"
          options={domainOpts}
          active={domain}
          onChange={(v) => {
            setDomain(v);
            setOffset(0);
          }}
        />
        <span className="ib-hosts-filter-sep" />
        <ChipFilter
          label="Filter by zone"
          options={ZONE_OPTS}
          active={zone}
          onChange={(v) => {
            setZone(v);
            setOffset(0);
          }}
        />
        <FilterInput
          label="Filter by resource type"
          placeholder="Filter by type…"
          value={type}
          width={170}
          onChange={(v) => {
            setType(v);
            setOffset(0);
          }}
        />
        <span style={{ marginLeft: "auto", fontSize: 11, fontFamily: FONT_MONO, color: MUTED }}>
          {pageWindow(offset, page.items.length, total, "resources")}
        </span>
      </div>

      <Panel
        rail={eolCount > 0 ? "warn" : "ok"}
        mnemonic="RESRC"
        description="raw collector resources · one row per domain that saw the host"
        live
        headerRight={
          <span style={{ fontSize: 11, fontFamily: FONT_MONO, color: MUTED }}>
            {domains.length} domains · {eolCount} EOL (page)
          </span>
        }
      >
        {page.items.length === 0 ? (
          filtersActive ? (
            <EmptyState
              kind="filter-zero"
              title="No resources match your filters"
              hint="Try a different domain or zone chip, or clear the type filter."
              action={{
                label: "Clear filters",
                onClick: () => {
                  setDomain("(all)");
                  setZone("(all)");
                  setType("");
                  setOffset(0);
                },
              }}
            />
          ) : (
            <EmptyState
              kind="none-yet"
              title="No resources collected yet"
              hint="Resources appear here after a domain agent completes its first sweep."
            />
          )
        ) : (
          <DataTable<Resource>
            columns={columns}
            rows={page.items}
            rowKey={(r) => r.id}
            toolbarName="resources"
            chips={[`${total} resources`, `${domains.length} domains (page)`]}
            sortable
            caption="Collector resources"
            statusBar={{ shown: page.items.length, total }}
            onRowClick={(r) => setSelected(r)}
          />
        )}
      </Panel>

      <Pager
        offset={offset}
        shown={page.items.length}
        total={total}
        pageSize={PAGE_SIZE}
        noun="resources"
        onOffset={setOffset}
      />

      <DetailDrawer
        title={selected ? `${selected.hostname} — ${selected.domain}` : "Resource detail"}
        open={selected !== null}
        onClose={() => setSelected(null)}
        triggerRef={triggerRef}
      >
        {selected && <ResourceDetail item={selected} onDriftClick={setDriftSelected} />}
      </DetailDrawer>

      {/* Cross-linked drift detail, opened from the resource drawer's related-
          drift list — self-contained so the link works without leaving the page. */}
      <DetailDrawer
        title={driftSelected ? `${driftSelected.field_name} — ${driftSelected.domain}` : "Drift detail"}
        open={driftSelected !== null}
        onClose={() => setDriftSelected(null)}
        triggerRef={driftTriggerRef}
      >
        {driftSelected && <DriftDetail item={driftSelected} />}
      </DetailDrawer>
    </div>
  );
}

/** Detail content for one resource: its facts, its snapshot timeline, and the
 *  drift events recorded against it.
 *
 *  A fetch that *failed* and a fetch that *succeeded with zero rows* are kept
 *  strictly distinct for both sub-fetches. Collapsing them (as the pre-2026-07
 *  version did) rendered a backend outage as "No drift detected — configuration
 *  stable." — a stability claim manufactured out of an error. */
function ResourceDetail({ item, onDriftClick }: { item: Resource; onDriftClick: (d: Drift) => void }) {
  const dn = item.drift_count ?? 0;
  const [snapshots, setSnapshots] = useState<Snapshot[] | null>(null);
  const [snapshotsError, setSnapshotsError] = useState<string | null>(null);
  const [relDrift, setRelDrift] = useState<Drift[] | null>(null);
  const [driftError, setDriftError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    setSnapshots(null);
    setSnapshotsError(null);
    setRelDrift(null);
    setDriftError(null);
    apiGet<unknown>(`/api/dashboard/resources/${item.id}/snapshots`)
      .then((d) => live && setSnapshots(rows<Snapshot>(d)))
      .catch((e) => {
        if (!live) return;
        if (e instanceof AuthRequired) redirectToLogin();
        else setSnapshotsError(String(e));
      });
    // The drift endpoint has no per-resource filter, so narrow by domain then
    // match hostname client-side (same approach as the legacy page).
    apiGet<unknown>(`/api/dashboard/drift_events?domain=${encodeURIComponent(item.domain)}`)
      .then((d) => live && setRelDrift(rows<Drift>(d).filter((x) => x.hostname === item.hostname)))
      .catch((e) => {
        if (!live) return;
        if (e instanceof AuthRequired) redirectToLogin();
        else setDriftError(String(e));
      });
    return () => {
      live = false;
    };
  }, [item.id, item.domain, item.hostname]);

  return (
    <div>
      <Panel className="ib-hosts-drawer-panel" rail={statusTone(item.status) === "err" ? "err" : "ok"} mnemonic="FACTS" description="collected resource facts">
        <KvGrid
          columns={2}
          fields={[
            { label: "Hostname", value: item.hostname },
            { label: "Domain", value: item.domain },
            { label: "Type", value: item.resource_type },
            { label: "Zone", value: item.zone },
            { label: "Status", value: <Badge tone={statusTone(item.status)}>{item.status}</Badge> },
            {
              label: "Open drift",
              value: dn > 0 ? <span style={{ color: statusColor("warn") }}>{dn} open</span> : "none",
            },
            { label: "Last seen", value: fmtDt(item.last_seen_at) },
            { label: "Resource id", value: <span style={{ color: FAINT }}>{item.id}</span> },
          ]}
        />
      </Panel>

      <Panel className="ib-hosts-drawer-panel" rail="info" mnemonic="SNAPS" description="snapshot history">
        {snapshotsError ? (
          <EmptyState kind="error" title="Snapshot history failed to load" hint={snapshotsError} />
        ) : snapshots === null ? (
          <Skeleton count={3} height={20} />
        ) : snapshots.length === 0 ? (
          <EmptyState kind="none-yet" title="No snapshots recorded." />
        ) : (
          <div>
            {snapshots.map((sn, i) => (
              <div key={i} style={{ display: "flex", alignItems: "center", gap: 12, padding: "8px 0" }}>
                <span
                  style={{
                    width: 8,
                    height: 8,
                    borderRadius: "50%",
                    flexShrink: 0,
                    background: i === 0 ? statusColor("ok") : FAINT,
                  }}
                />
                <span style={{ flex: 1, fontSize: 12, fontFamily: FONT_MONO, color: TEXT }}>{fmtDt(sn.ts)}</span>
                <Badge tone="neutral">{sn.label}</Badge>
              </div>
            ))}
          </div>
        )}
      </Panel>

      <Panel
        className="ib-hosts-drawer-panel"
        rail={relDrift && relDrift.length > 0 ? "warn" : "ok"}
        mnemonic="DRIFT"
        description={driftError ? "drift history" : `drift history (${relDrift?.length ?? 0})`}
      >
        {driftError ? (
          <EmptyState kind="error" title="Drift history failed to load" hint={driftError} />
        ) : relDrift === null ? (
          <Skeleton count={2} height={44} />
        ) : relDrift.length === 0 ? (
          <EmptyState kind="none-yet" title="No drift detected — configuration stable." />
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {relDrift.map((d) => (
              <button
                key={d.id}
                type="button"
                onClick={() => onDriftClick(d)}
                style={{
                  all: "unset",
                  cursor: "pointer",
                  display: "block",
                  border: "1px solid var(--border)",
                  borderLeft: `2px solid ${normalizeStatus(d.status) === "ok" ? "transparent" : statusColor("warn")}`,
                  borderRadius: 3,
                  background: "var(--raised)",
                  padding: "10px 12px",
                  fontFamily: FONT_MONO,
                }}
              >
                <span style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
                  <span style={{ fontSize: 12, fontWeight: 600, color: TEXT }}>{d.field_name}</span>
                  <Badge tone={driftStatusTone(d.status)}>{d.status}</Badge>
                </span>
                <span style={{ display: "block", fontSize: 11, marginTop: 6 }}>
                  <span style={{ color: MUTED }}>{d.old_value}</span> <span style={{ color: FAINT }}>→</span>{" "}
                  <span style={{ color: TEXT }}>{d.new_value}</span>
                </span>
                <span style={{ display: "block", fontSize: 10, color: FAINT, marginTop: 6 }}>{fmtDt(d.detected_at)}</span>
              </button>
            ))}
          </div>
        )}
      </Panel>
    </div>
  );
}

/** Drift detail: the before/after pair, the identifying details, and an
 *  explanation of what the drift type means. Opened as a second drawer from the
 *  resource drawer's related-drift list. */
function DriftDetail({ item }: { item: Drift }) {
  const hasJira = item.jira_ticket && item.jira_ticket !== "—";
  const note =
    item.drift_type === "state_drift"
      ? "State drift means this asset was present in a prior collection run but absent from the latest sweep. The detector flags disappeared resources automatically."
      : "Config drift means a tracked field changed between two consecutive snapshots. If an instinct explains the change it may be auto-classified; otherwise a Jira ticket is opened for review.";

  return (
    <div>
      <div style={{ display: "flex", gap: 8, marginBottom: 14, flexWrap: "wrap" }}>
        <Badge tone={driftStatusTone(item.status)}>{item.status}</Badge>
        {hasJira && <Badge tone="info">{item.jira_ticket}</Badge>}
      </div>

      <Panel className="ib-hosts-drawer-panel" rail="warn" mnemonic="CHANGE" description="before → after">
        <SectionLabel>Before</SectionLabel>
        <div
          style={{
            fontSize: 13,
            fontFamily: FONT_MONO,
            color: TEXT,
            wordBreak: "break-word",
            padding: "10px 12px",
            border: `1px solid ${statusColor("error")}`,
            borderRadius: 3,
            marginBottom: 10,
          }}
        >
          {item.old_value}
        </div>
        <SectionLabel>After</SectionLabel>
        <div
          style={{
            fontSize: 13,
            fontFamily: FONT_MONO,
            color: TEXT,
            wordBreak: "break-word",
            padding: "10px 12px",
            border: `1px solid ${statusColor("ok")}`,
            borderRadius: 3,
          }}
        >
          {item.new_value}
        </div>
      </Panel>

      <Panel className="ib-hosts-drawer-panel" rail="info" mnemonic="DETAIL" description="drift metadata">
        <KvGrid
          columns={2}
          fields={[
            { label: "Affected resource", value: item.hostname },
            { label: "Drift type", value: driftTypeLabel(item.drift_type) },
            { label: "Detected", value: fmtDt(item.detected_at) },
            { label: "Jira ticket", value: item.jira_ticket || "—" },
            { label: "Root cause", value: item.root_cause || "—" },
          ]}
        />
        <div style={{ fontSize: 11, color: MUTED, lineHeight: 1.6, marginTop: 16 }}>{note}</div>
      </Panel>
    </div>
  );
}
